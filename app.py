import json
import asyncio
import time
import os
import logging
import hashlib
import hmac
import socket
import threading
try:
    import winreg  # Windows-only; None on Linux/macOS
except ImportError:
    winreg = None
import subprocess
from logging.handlers import RotatingFileHandler
from pathlib import Path
from contextlib import asynccontextmanager
from collections import deque
from typing import Optional

from fastapi import FastAPI, Request, HTTPException, Depends
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from fastapi.templating import Jinja2Templates
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from pydantic import BaseModel
import httpx
import secrets
import webbrowser
import re
import copy

# ============================================================
# 日志
# ============================================================
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
)
logger = logging.getLogger("reservoir")

# ============================================================
# 路径与常量
# ============================================================
import sys

# ============================================================
# 输入法自愈（中文输入）
# ============================================================
# 桌面会话常漏设 GTK_IM_MODULE（QT_IM_MODULE/XMODIFIERS 都有，唯独 GTK 没有），
# GTK3 找不到模块就退回内置简单输入法 → 界面上一个中文字都打不出来。
# 且打包版还有一坑：PyInstaller 的 pyi_rth_gtk.py 会把
# GTK_PATH/GTK_EXE_PREFIX/GTK_DATA_PREFIX 全指向包内（_MEI），包里没有
# immodules.cache → GTK 一个 IM 模块都找不到，只补 GTK_IM_MODULE 也无效。
# GTK3 支持 GTK_IM_MODULE_FILE 直指缓存文件，补上它即可（实验验证：
# 打包前缀下 FCITX_LOADED=False，加本变量后 True）。
# 必须在 import webview / GTK 初始化之前执行（IM 模块按 env 惰性加载）。
def ensure_gtk_im_module(env, exists=os.path.exists):
    """补齐 GTK3 中文输入所需环境变量。返回是否补设了 GTK_IM_MODULE。

    1) GTK_IM_MODULE——选哪个 IM 模块（fcitx/ibus）；
    2) GTK_IM_MODULE_FILE——模块缓存 immodules.cache 的位置（打包版必需）。
    两件事独立补齐：环境可能只缺其一。
    """
    if sys.platform != "linux":
        return False
    changed = False
    if not env.get("GTK_IM_MODULE"):
        for name, paths in (
            ("fcitx", ("/usr/lib/gtk-3.0/3.0.0/immodules/im-fcitx5.so",
                       "/usr/lib64/gtk-3.0/3.0.0/immodules/im-fcitx5.so",
                       "/usr/lib/gtk-3.0/3.0.0/immodules/im-fcitx.so",
                       "/usr/lib64/gtk-3.0/3.0.0/immodules/im-fcitx.so")),
            ("ibus", ("/usr/lib/gtk-3.0/3.0.0/immodules/im-ibus.so",
                      "/usr/lib64/gtk-3.0/3.0.0/immodules/im-ibus.so")),
        ):
            if any(exists(p) for p in paths):
                env["GTK_IM_MODULE"] = name
                changed = True
                break
    if not env.get("GTK_IM_MODULE_FILE"):
        for p in ("/usr/lib/gtk-3.0/3.0.0/immodules.cache",
                  "/usr/lib64/gtk-3.0/3.0.0/immodules.cache",
                  "/usr/lib/x86_64-linux-gnu/gtk-3.0/3.0.0/immodules.cache",
                  "/usr/lib/aarch64-linux-gnu/gtk-3.0/3.0.0/immodules.cache"):
            if exists(p):
                env["GTK_IM_MODULE_FILE"] = p
                break
    return changed


ensure_gtk_im_module(os.environ)

# 控制台编码统一 UTF-8：英文/非中文区位 Windows（cp1252 等）打印中文日志会
# UnicodeEncodeError 直接崩，中文 Windows(GBK) 碰不到，CI/海外机器必踩
for _stream in (sys.stdout, sys.stderr):
    _reconf = getattr(_stream, "reconfigure", None) if _stream is not None else None
    if callable(_reconf):
        try:
            _reconf(encoding="utf-8", errors="replace")
        except Exception:
            pass

if getattr(sys, 'frozen', False):
    APP_DIR = Path(sys._MEIPASS)
    # 打包版也认 GATEWAY_DATA_DIR：测试实例、便携模式、多开都靠它
    # （以前只有开发模式认，打包版强制跟 exe 走，集成测试只能污染 dist/）。
    _env_data = os.environ.get("GATEWAY_DATA_DIR", "").strip()
    if _env_data:
        DATA_DIR = Path(_env_data).expanduser()
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    elif sys.platform == "darwin":
        # macOS: .app bundle 内不可写，数据放 Application Support
        DATA_DIR = Path.home() / "Library" / "Application Support" / "ModelGateway"
        DATA_DIR.mkdir(parents=True, exist_ok=True)
    else:
        DATA_DIR = Path(sys.executable).parent
else:
    APP_DIR = Path(__file__).parent
    # 开发模式支持环境变量 GATEWAY_DATA_DIR 指向真实数据目录（如 dist/）
    _env_data = os.environ.get("GATEWAY_DATA_DIR", "")
    if _env_data:
        DATA_DIR = Path(_env_data) if Path(_env_data).is_absolute() else Path(__file__).parent / _env_data
    else:
        DATA_DIR = Path(__file__).parent

DATA_FILE = DATA_DIR / "providers.json"
CONFIG_FILE = DATA_DIR / "config.json"
HISTORY_FILE = DATA_DIR / "history.jsonl"
USAGE_FILE = DATA_DIR / "usage.jsonl"
META_FILE = DATA_DIR / "models_meta.json"
ROUTERS_FILE = DATA_DIR / "routers.json"
ANNOUNCEMENT_FILE = DATA_DIR / "announcement.json"

APP_VERSION = "1.9.2"

MAX_HISTORY_DAYS = 30
MAX_USAGE_DAYS = 30        # 按天数范围查询(近24小时/7天/30天)的上限
USAGE_RETENTION_DAYS = 3650  # 消耗记录保留期：10年，"全部"按钮才有意义
HISTORY_CLEANUP_INTERVAL = 6 * 3600
ONE_MILLION = 1048576
POLL_INTERVAL = 300
CIRCUIT_FAIL_THRESHOLD = 3
CIRCUIT_RECOVERY_SECONDS = 60
QUALITY_WINDOW = 20
POLL_MAX_COUNT = 20
CALL_LOG_MAX = 10000  # 内存队列事实上无上限（正常使用远达不到；防异常刷量撑爆内存）


# ============================================================
# 原子写入
# ============================================================
def atomic_write(path: Path, content: str):
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(path)


# ============================================================
# 配置加载
# ============================================================
def load_config():
    if CONFIG_FILE.exists():
        data = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        # 补充缺失的默认字段
        data.setdefault("local_api_key", "sk-local-" + secrets.token_hex(16))
        data.setdefault("port", 8000)
        data.setdefault("poll_count", 0)
        data.setdefault("last_daily_poll", "")
        return data
    data = {
        "local_api_key": "sk-local-" + secrets.token_hex(16),
        "port": 8000,
        "poll_count": 0,
        "last_daily_poll": "",
    }
    atomic_write(CONFIG_FILE, json.dumps(data, indent=2))
    return data


def save_config():
    """将内存中的 app_config 原子写回 config.json。"""
    atomic_write(CONFIG_FILE, json.dumps(app_config, ensure_ascii=False, indent=2))


def load_providers():
    if DATA_FILE.exists():
        return json.loads(DATA_FILE.read_text(encoding="utf-8"))
    return []


def save_providers(data):
    atomic_write(DATA_FILE, json.dumps(data, ensure_ascii=False, indent=2))


def load_meta():
    """三层合并：内置兜底(APP_DIR/models_meta.json) → 外部覆盖(DATA_DIR/models_meta.json)。
    dict 字段深合并，其余字段直接覆盖。"""
    default = {
        "aliases": {},
        "context_limits": {},
        "non_chat_keywords": [],
        "model_descriptions": {},
        "supports_vision": {},
        "modalities": {},
    }
    # 内置版（打包内嵌进 exe，断网保底；非打包时与外部版同路径）
    builtin = APP_DIR / "models_meta.json"
    if builtin.exists():
        try:
            default.update(json.loads(builtin.read_text(encoding="utf-8")))
        except Exception:
            pass
    # 外部版（exe 同目录，用户可覆盖/补充）
    if META_FILE.exists():
        try:
            ext = json.loads(META_FILE.read_text(encoding="utf-8"))
            for k, v in ext.items():
                if isinstance(v, dict) and isinstance(default.get(k), dict):
                    default[k].update(v)
                else:
                    default[k] = v
        except Exception:
            pass
    return default


def load_routers():
    if ROUTERS_FILE.exists():
        try:
            return json.loads(ROUTERS_FILE.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    return {}

def save_routers():
    atomic_write(ROUTERS_FILE, json.dumps(ROUTERS, indent=2, ensure_ascii=False))


app_config = load_config()
LOCAL_API_KEY = app_config.get("local_api_key")
ROUTERS = load_routers()

meta = load_meta()
MODEL_ALIASES = meta.get("aliases", {})
CONTEXT_LIMITS = meta.get("context_limits", {})
NON_CHAT_KEYWORDS = meta.get("non_chat_keywords", [])
MODEL_DESCRIPTIONS = meta.get("model_descriptions", {})
SUPPORTS_VISION = meta.get("supports_vision", {})
MODEL_MODALITIES = meta.get("modalities", {})


# ============================================================
# 鉴权
# ============================================================
security = HTTPBearer(auto_error=False)


def verify_client(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """客户端调用 /v1/* 的鉴权（本机对接工具用）"""
    if not credentials:
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    # 恒定时间比较，理由同 verify_admin：`!=` 在首个不匹配字节处提前返回，
    # 同机其他进程可据此逐字节推测密钥。两个鉴权函数必须用同一套比较方式 ——
    # 上一轮只改了 verify_admin，漏了这条对外的主路径。
    if not LOCAL_API_KEY or not hmac.compare_digest(
            credentials.credentials.encode("utf-8"), LOCAL_API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid or missing API Key")
    return credentials


def verify_admin(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """管理面板调用 /api/* 的鉴权，直接使用 local_api_key"""
    if not credentials:
        raise HTTPException(status_code=401, detail="Missing credentials")
    # 恒定时间比较：`!=` 会在首个不匹配字节处提前返回，攻击者据此逐字节
    # 推测密钥（时序攻击）。本地 API 不经公网，但仍是同一台机器上的其他进程可打。
    if not LOCAL_API_KEY or not hmac.compare_digest(
            credentials.credentials.encode("utf-8"), LOCAL_API_KEY.encode("utf-8")):
        raise HTTPException(status_code=401, detail="Invalid API key")
    return credentials


# ============================================================
# 断电恢复：检查一键配置的残留备份，自动还原
# ============================================================
def _recover_preset_backup():
    bak_p = DATA_FILE.with_suffix(DATA_FILE.suffix + ".preset_bak")
    bak_r = ROUTERS_FILE.with_suffix(ROUTERS_FILE.suffix + ".preset_bak")
    recovered = False
    if bak_p.exists():
        try:
            atomic_write(DATA_FILE, bak_p.read_text("utf-8"))
            bak_p.unlink()
            recovered = True
            logger.warning("断电恢复：已从备份还原 providers.json")
        except Exception:
            logger.exception("备份还原 providers.json 失败")
    if bak_r.exists():
        try:
            atomic_write(ROUTERS_FILE, bak_r.read_text("utf-8"))
            bak_r.unlink()
            recovered = True
            logger.warning("断电恢复：已从备份还原 routers.json")
        except Exception:
            logger.exception("备份还原 routers.json 失败")
    return recovered

_recover_preset_backup()

# ============================================================
# 全局状态
# ============================================================
providers = load_providers()
health_status: dict = {}
model_details: dict = {}
model_quality: dict = {}          # key -> {ok, fail, error, latencies: deque}
circuit_breaker: dict = {}        # key -> {fails, open_until}
providers_lock = asyncio.Lock()
history_lock = asyncio.Lock()
usage_lock = asyncio.Lock()
http_client: httpx.AsyncClient | None = None
poll_task = None
last_poll_time: float = 0
last_check_time: float = time.time()
last_history_cleanup: float = 0

# 调用日志（内存队列）
call_log = deque(maxlen=CALL_LOG_MAX)

# 轮询计数（从 config 加载，poll_all 里增量更新）
poll_count_state = app_config.get("poll_count", 0)
last_daily_poll_state = app_config.get("last_daily_poll", "")
poll_stage = "init"  # init | waiting | fetching_models | running | idle


def mark_full_check():
    """记录一次完整检测的时间，用于重置自动轮询计时"""
    global last_check_time
    last_check_time = time.time()


# ============================================================
# 历史记录（异步文件 IO）
# ============================================================
def _append_history_sync(snapshot: dict):
    line = json.dumps({"time": time.time(), "data": snapshot}, ensure_ascii=False) + "\n"
    with open(HISTORY_FILE, "a", encoding="utf-8") as f:
        f.write(line)


async def append_history(snapshot: dict):
    await asyncio.to_thread(_append_history_sync, snapshot)


def _read_history_sync(hours: int):
    if not HISTORY_FILE.exists():
        return []
    cutoff = time.time() - hours * 3600
    records = []
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec["time"] >= cutoff:
                    records.append(rec)
            except Exception:
                pass
    return records


async def read_history(hours: int = 24):
    async with history_lock:
        return await asyncio.to_thread(_read_history_sync, hours)


def _cleanup_history_sync():
    if not HISTORY_FILE.exists():
        return 0
    cutoff = time.time() - MAX_HISTORY_DAYS * 86400
    kept = []
    removed = 0
    with open(HISTORY_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec["time"] >= cutoff:
                    kept.append(line if line.endswith("\n") else line + "\n")
                else:
                    removed += 1
            except Exception:
                pass
    if removed > 0:
        atomic_write(HISTORY_FILE, "".join(kept))
    return removed


async def maybe_cleanup_history():
    global last_history_cleanup
    now = time.time()
    if now - last_history_cleanup < HISTORY_CLEANUP_INTERVAL:
        return
    last_history_cleanup = now
    n = await asyncio.to_thread(_cleanup_history_sync)
    if n:
        logger.info("history cleanup: removed %d expired records", n)
    un = await asyncio.to_thread(_cleanup_usage_sync)
    if un:
        logger.info("usage cleanup: removed %d expired records", un)


# ============================================================
# 消耗统计（异步文件 IO）
# ============================================================
def _append_usage_sync(record: dict):
    line = json.dumps(record, ensure_ascii=False) + "\n"
    with open(USAGE_FILE, "a", encoding="utf-8") as f:
        f.write(line)


async def append_usage(record: dict):
    await asyncio.to_thread(_append_usage_sync, record)


def _read_usage_sync(days: int):
    if not USAGE_FILE.exists():
        return []
    # days<=0 = "全部"：不按时间截断
    cutoff = time.time() - days * 86400 if days > 0 else 0
    records = []
    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec.get("ts", 0) >= cutoff:
                    records.append(rec)
            except Exception:
                pass
    return records


async def read_usage(days: int = 1):
    async with usage_lock:
        return await asyncio.to_thread(_read_usage_sync, days)


def _cleanup_usage_sync():
    if not USAGE_FILE.exists():
        return 0
    cutoff = time.time() - USAGE_RETENTION_DAYS * 86400
    kept = []
    removed = 0
    with open(USAGE_FILE, "r", encoding="utf-8") as f:
        for line in f:
            try:
                rec = json.loads(line.strip())
                if rec.get("ts", 0) >= cutoff:
                    kept.append(line if line.endswith("\n") else line + "\n")
                else:
                    removed += 1
            except Exception:
                pass
    if removed > 0:
        atomic_write(USAGE_FILE, "".join(kept))
    return removed


# ============================================================
# 模型工具函数
# ============================================================
def is_chat_model(model_id: str) -> bool:
    lower = model_id.lower()
    return not any(kw in lower for kw in NON_CHAT_KEYWORDS)


def is_free_model(model_info: dict) -> bool:
    pricing = model_info.get("pricing", {})
    prompt_price = pricing.get("prompt", "")
    completion_price = pricing.get("completion", "")
    try:
        if float(prompt_price) == 0 and float(completion_price) == 0:
            return True
    except (ValueError, TypeError):
        pass
    return False


def is_free_by_name(model_id: str) -> bool:
    lower = model_id.lower()
    return ":free" in lower or "-free" in lower


# ============================================================
# 免费/付费判定：证据分级（2026-10-15）
# ============================================================
# 老逻辑的问题：「仅免费」标签直接渲染配置位 free_only（添加时前端写死 true），
# 而 fetch_models 在上游既不给 pricing、模型名也不含 -free 时会兜底返回全部模型，
# 于是 deepseek 这种明确按量计费的上游也被标成"仅免费"——标签在撒谎。
# 改为按证据强度分级判定，没有证据就老实说「未核实」：
#   E1 上游 /models 自带 pricing（厂商自己报的，最权威）
#   E2 模型名以 :free / -free 结尾（OpenRouter/Groq 系命名惯例）
#   E3 base_url 是本机或内网（自建部署，没有按量计费；电费另算）
#   E4 公开价目源（OpenRouter /models，7 天缓存）按归一化名命中
#   全无证据 → unknown
# 冲突时以更强证据为准，但打 conflict 标记，前端 tooltip 里会说人话。
FREE_NAME_SUFFIXES = (":free", "-free", "/free", "_free")
PRICE_ORACLE_URL = "https://openrouter.ai/api/v1/models"
PRICE_ORACLE_TTL = 7 * 86400  # 秒；价目缓存有效期
_LOOPBACK_HOSTS = {"localhost", "127.0.0.1", "::1", "0.0.0.0"}


def has_free_name(model_id: str) -> bool:
    """模型名是否带免费后缀（只看末段，避免把 供应商/free-xxx 之类的前缀误判）。"""
    tail = model_id.lower().split("/")[-1]
    return any(tail.endswith(sfx) for sfx in FREE_NAME_SUFFIXES)


def _url_host(base_url: str) -> str:
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/:?#]+)", (base_url or "").strip())
    return m.group(1).lower() if m else ""


def is_local_base_url(base_url: str) -> bool:
    """本机/内网地址 → 自建部署，没有按量计费。"""
    host = _url_host(base_url)
    if not host:
        return False
    if host in _LOOPBACK_HOSTS or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(p.isdigit() for p in parts):
        a, b = int(parts[0]), int(parts[1])
        if a == 10 or (a == 192 and b == 168) or (a == 169 and b == 254):
            return True
        if a == 172 and 16 <= b <= 31:
            return True
    return False


def _parse_price(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def classify_model_free(
    model_id: str,
    *,
    upstream_pricing: dict | None = None,
    base_url: str = "",
    oracle: dict | None = None,
    local: bool | None = None,
) -> dict:
    """按证据分级判定单个模型是否免费。

    返回 {"status": "free|paid|unknown", "source": "upstream|name|local|oracle|none",
          "price": [输入单价, 输出单价] 或 None, "conflict": bool}
    """
    if local is None:
        local = is_local_base_url(base_url)
    oracle_hit = None
    if isinstance(oracle, dict):
        oracle_hit = (oracle.get("tail") or {}).get(model_id.lower().split("/")[-1])

    # E1 上游自报 pricing（最权威）
    if isinstance(upstream_pricing, dict):
        ip = _parse_price(upstream_pricing.get("prompt"))
        op = _parse_price(upstream_pricing.get("completion"))
        if ip is not None and op is not None:
            verdict = "free" if ip == 0 and op == 0 else "paid"
            conflict = bool(oracle_hit) and (
                (verdict == "free" and (oracle_hit[0] > 0 or oracle_hit[1] > 0))
                or (verdict == "paid" and oracle_hit[0] == 0 and oracle_hit[1] == 0)
            )
            return {"status": verdict, "source": "upstream", "price": [ip, op], "conflict": conflict}

    # E2 命名惯例
    if has_free_name(model_id):
        return {"status": "free", "source": "name", "price": None, "conflict": False}

    # E3 本机/内网自建
    if local:
        return {"status": "free", "source": "local", "price": None, "conflict": False}

    # E4 公开价目源
    if oracle_hit:
        ip, op = float(oracle_hit[0]), float(oracle_hit[1])
        return {
            "status": "free" if ip == 0 and op == 0 else "paid",
            "source": "oracle",
            "price": [ip, op],
            "conflict": False,
        }

    return {"status": "unknown", "source": "none", "price": None, "conflict": False}


# ---------- 公开价目源（只读、可关、失败不致命） ----------
def _price_cache_path() -> Path:
    return DATA_DIR / "pricing_cache.json"


def _price_oracle_enabled() -> bool:
    return os.environ.get("GATEWAY_PRICE_ORACLE", "1").lower() not in ("0", "false", "no")


def _read_price_cache() -> dict:
    """读价目缓存；文件缺失/损坏/结构不对一律当没缓存，绝不抛。"""
    try:
        raw = json.loads(_price_cache_path().read_text(encoding="utf-8"))
        tail = raw.get("tail")
        if isinstance(tail, dict):
            return {
                "tail": {
                    str(k).lower(): [float(v[0]), float(v[1])]
                    for k, v in tail.items()
                    if isinstance(v, (list, tuple)) and len(v) == 2
                },
                "fetched_at": float(raw.get("fetched_at") or 0),
                "source": raw.get("source") or "openrouter",
            }
    except Exception:
        pass
    return {"tail": {}, "fetched_at": 0.0, "source": "openrouter"}


def _write_price_cache(data: dict) -> None:
    try:
        _price_cache_path().write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
    except Exception:
        logger.exception("写价目缓存失败")


async def _fetch_price_oracle() -> dict | None:
    """拉公开价目源（无需 key，只读）。失败返回 None，绝不抛。"""
    if http_client is None:
        return None
    try:
        resp = await http_client.get(PRICE_ORACLE_URL, timeout=15)
        if resp.status_code != 200:
            return None
        payload = resp.json()
        items = payload.get("data") if isinstance(payload, dict) else payload
        if not isinstance(items, list):
            return None
        tail: dict[str, list] = {}
        for m in items:
            if not isinstance(m, dict):
                continue
            mid = str(m.get("id", "")).lower()
            pricing = m.get("pricing") or {}
            ip = _parse_price(pricing.get("prompt"))
            op = _parse_price(pricing.get("completion"))
            if not mid or ip is None or op is None:
                continue
            tail[mid.split("/")[-1]] = [ip, op]
        if not tail:
            return None
        return {"tail": tail, "fetched_at": time.time(), "source": "openrouter"}
    except Exception:
        logger.exception("价目源拉取失败")
        return None


async def _bg_refresh_price_oracle() -> None:
    try:
        fresh = await _fetch_price_oracle()
        if fresh:
            _write_price_cache(fresh)
    except Exception:
        logger.exception("后台刷新价目源失败")


async def get_price_oracle(refresh_if_stale: bool = False) -> dict:
    """拿价目源。默认只读缓存（过期就在后台悄悄刷，页面不等网络）。"""
    data = _read_price_cache()
    if not _price_oracle_enabled():
        return data
    if (time.time() - data["fetched_at"]) > PRICE_ORACLE_TTL:
        if refresh_if_stale:
            fresh = await _fetch_price_oracle()
            if fresh:
                _write_price_cache(fresh)
                return fresh
        else:
            try:
                asyncio.create_task(_bg_refresh_price_oracle())
            except RuntimeError:
                pass  # 没有运行中的事件循环（单测同步调用路径）
    return data


def summarize_provider_free(provider: dict, oracle: dict | None = None) -> dict:
    """提供商级免费概览：只统计启用中的模型，按证据分级。

    badge：paid（含付费）> unknown（未核实）> free（仅免费）——
    只有全部启用模型都验证为免费，才敢说「仅免费」。
    """
    override = str(provider.get("free_status_override") or "auto").strip().lower()
    if oracle is None:
        oracle = _read_price_cache()
    models = get_enabled_models(provider)
    upstream = provider.get("model_pricing") or {}
    base_url = provider.get("base_url", "")
    counts = {"free": 0, "paid": 0, "unknown": 0}
    sources: dict[str, int] = {}
    conflicts = 0
    for m in models:
        v = classify_model_free(
            m, upstream_pricing=upstream.get(m), base_url=base_url, oracle=oracle
        )
        counts[v["status"]] += 1
        sources[v["source"]] = sources.get(v["source"], 0) + 1
        if v["conflict"]:
            conflicts += 1
    badge = "paid" if counts["paid"] else ("unknown" if counts["unknown"] else "free")
    if override in ("free", "paid", "unknown"):
        badge = f"override:{override}"
    return {
        "badge": badge,
        "override": override,
        "models_total": len(models),
        **counts,
        "conflicts": conflicts,
        "sources": sources,
    }


def get_enabled_models(provider: dict) -> list[str]:
    """返回该 provider 未被禁用的模型列表"""
    disabled = set(provider.get("disabled_models", []))
    return [m for m in provider.get("models", []) if m not in disabled]


def normalize_model(model: str) -> str:
    """归一化模型名到标准名（去 xxx/ 前缀 + 转小写）。
    先查别名表做名字修正（如 mistral-small-2603 → mistral-small-4-119b-2603），
    再统一去前缀转小写。这样不同平台/大小写命名都归一到同一标准名。"""
    if model in MODEL_ALIASES:
        model = MODEL_ALIASES[model]
    return model.split('/')[-1].lower()


# ---------- 对外模型名写法（统一规范：供应商/模型名） ----------
MODEL_ID_SEP = "/"


def local_model_id(provider_name: str, model: str) -> str:
    """对外模型名的唯一拼法：`供应商/模型名`（斜杠分隔）。

    这是用户拍定的规则，任何面向客户端或界面的地方都必须走本函数，
    不要再手写 f"{provider}-{model}" 或 f"{provider} · {model}" 之类的临时拼法。
    """
    return f"{provider_name}{MODEL_ID_SEP}{model}"


def model_id_matches(requested: str, provider_name: str, model: str) -> bool:
    """判断请求里的 model 是否指向 (供应商, 模型名)。

    接受三种写法：裸模型名、标准 `供应商/模型名`、历史 `供应商-模型名`
    （旧写法保留兼容，避免已配好的第三方客户端挂掉）。
    """
    if not requested:
        return False
    return requested in (model,
                         f"{provider_name}{MODEL_ID_SEP}{model}",
                         f"{provider_name}-{model}")


def get_context_length(model: str) -> int:
    # ① 归一化名查表
    norm = normalize_model(model)
    ctx = CONTEXT_LIMITS.get(norm) or CONTEXT_LIMITS.get(model)
    if ctx:
        return ctx
    # ② 大小写回退（防标准名表里仍存了带前缀/带大小写的旧键）
    lower = model.lower()
    for k, v in CONTEXT_LIMITS.items():
        if k.lower() == lower:
            return v
    # ③ 轮询拉取的 model_details
    return (model_details.get(norm, {}).get("context_length")
            or model_details.get(model, {}).get("context_length")
            or 32768)


def is_vision_model(model: str) -> bool:
    """是否支持识图（基于 supports_vision 标记 + 归一化匹配）"""
    norm = normalize_model(model)
    return bool(SUPPORTS_VISION.get(norm) or SUPPORTS_VISION.get(model))


# 模态能力类别（key 供 API/前端共用；labels 仅后端校验显式覆盖合法性）
MODALITY_CATEGORIES = {
    "text": "纯文本",
    "vision": "图像理解",
    "audio_in": "语音理解",
    "image_gen": "图像生成",
    "audio_gen": "音频生成",
    "video_gen": "视频生成",
    "omni": "全模态",
    "router": "路由组",
}

# ------------------------------------------------------------------
# 模型能力知识库（家族级，按官方模型卡于 2026-10 核对）
# 每行 = (正则, 模态 key, 是否支持深度思考)；对归一化模型名 re.search，按序首命中即返回。
# 排序铁律：越具体、越易被泛化规则误伤的行必须越靠前 ——
#   ① 生成类（图像/视频/音频）最特异，最先拦；
#   ② 同族特例（gpt-4o-mini、minimax-m1、qwen3-vl-*-thinking、nemotron-3-nano-omni）先于其家族宽泛规则；
#   ③ 全模态 omni 先于通用 vision，vision 先于默认文本。
# 误判仍可能（模型迭代快），故保留 models_meta.modalities 人工覆盖 + 前端一键纠偏。
# ------------------------------------------------------------------
MODEL_CAPABILITY_KB = (
    # ① 生成类：吐图像 / 视频 / 音频
    (r"dall-?e|gpt-image|imagen|stable-diffusion|sdxl|midjourney|flux|kolors|cogview|seedream|wanx|text2image|text-to-image|image-gen|sensenova-u1", "image_gen", False),
    (r"sora|kling|vidu|hailuo|mochi|\bveo\b|t2v|text-to-video|video-gen|cogvideox|runway|seedance", "video_gen", False),
    (r"\btts\b|-tts|speech-synth|voice-?clone|voice-?design|cosyvoice|fish-speech|musicgen|suno|lyria|elevenlabs|text-to-speech|audio-gen|speech-gen", "audio_gen", False),
    (r"\basr\b|-asr|speech-to-text|whisper|sensevoice|paraformer|audio2text|speech-recognition|audio-in|audio-understand", "audio_in", False),
    # 向量 / 重排模型：吃文本吐向量，最先拦，防被后行 omni/vision 带偏
    (r"embedding|embed-|rerank|bge-|gte-|compassjudger", "text", False),
    # ② 同族特例：能力与家族默认不同，须先于家族宽泛规则
    (r"minimax-m1", "text", True),                  # M1：文本+推理
    (r"qwen3-vl.*thinking", "vision", True),        # Qwen3-VL Thinking：视觉+思考
    (r"glm-5|glm-4\.7", "text", True),              # GLM-5.x / GLM-4.7：文本+思考
    (r"nemotron-3-nano-omni", "omni", True),        # 名字已含 omni，显式提权
    (r"gpt-4o-mini", "vision", False),              # 只吃文本+图像，须先于 gpt-4o 的 omni 规则
    # ③ 全模态理解（文本+图像+音频[+视频]）
    (r"omni", "omni", True),
    (r"gpt-4o|gpt-4\.1|gpt-5|gemini|multimodal|multi-modal", "omni", True),
    (r"gemma-4", "omni", True),                     # Gemma 4：文本/图像/音频/视频
    (r"kimi-k3", "omni", True),                     # Kimi K3：原生多模态
    (r"mimo-v2\.6", "omni", True),                  # MiMo v2.6 pro/flash：全模态理解
    (r"mimo-v2\.5", "omni", True),                  # MiMo v2.5 / v2.5-Pro：文本+图像+视频+音频全模态理解（官方模型卡）
    (r"qwen3\.8-omni", "omni", True),
    # ④ 视觉理解（文本+图像）
    (r"deepseek-v4|deepseek-flash", "vision", True),   # V4 系列（Pro 正式版 / V4.1 Flash）：原生多模态视觉
    (r"qwen3\.5|qwen3\.8", "vision", True),            # Qwen3.5/3.8：原生多模态+思考
    (r"kimi-k2", "vision", True),                      # K2.6：文本+图像+视频输入
    (r"minimax-m3", "vision", True),                   # M3：原生多模态
    (r"sensenova-6", "vision", True),
    (r"nex-n2", "vision", True),
    (r"step-3", "vision", True),
    (r"intern-s\d", "vision", True),                   # Intern-S1 / S2
    (r"gemma-3", "vision", False),
    (r"mistral-(small-4|large-3|medium-3)", "vision", False),
    (r"ernie-4\.5-vl|glm-4\.5v|glm-4v|-vl|_vl|vision|llava|internvl|qvq|minicpm-v", "vision", False),
    # ⑤ 纯文本 + 深度思考（思考链 / reasoning 家族）
    (r"nemotron-3|gpt-oss|deepseek-r1|reasoner|-r1\b|reasoning|thinking|qwq|qwen3.*think", "text", True),
    # ⑥ 兜底：纯文本（见 infer_capabilities 末尾）
)

# 图像理解的名称兜底（supports_vision 未收录但命名可辨）——仅 KB 未命中时生效
MODALITY_VISION_HINTS = ("vision", "llava", "internvl", "qvq", "minicpm-v",
                         "-vl", "_vl", "vl-", "vl_")


def _explicit_capability(model: str):
    """读取 models_meta.modalities 的人工覆盖，返回 (modality, reasoning)；无覆盖返回 (None, None)。

    兼容两种写法：字符串 "vision"（只管模态）或对象 {"modality": ..., "reasoning": ...}。
    非法模态值一律忽略（防手误写死），reasoning 非布尔也忽略并回退自动判定。
    """
    entry = MODEL_MODALITIES.get(normalize_model(model)) or MODEL_MODALITIES.get(model)
    if isinstance(entry, str):
        return (entry, None) if entry in MODALITY_CATEGORIES else (None, None)
    if isinstance(entry, dict):
        mod = entry.get("modality")
        reason = entry.get("reasoning")
        return (mod if mod in MODALITY_CATEGORIES else None,
                reason if isinstance(reason, bool) else None)
    return (None, None)


def infer_capabilities(model: str) -> dict:
    """判定模型能力，返回 {"modality": <key>, "reasoning": bool}。

    优先级：models_meta.modalities 人工覆盖 > 家族知识库 MODEL_CAPABILITY_KB >
    supports_vision 策展 > 命名兜底 > 默认纯文本。
    人工覆盖里未显式给出的维度继续走自动判定（可只纠偏其中一项）。
    """
    norm = normalize_model(model)
    explicit_mod, explicit_reason = _explicit_capability(model)

    modality, reasoning = None, False
    for pattern, kind, reason in MODEL_CAPABILITY_KB:
        if re.search(pattern, norm):
            modality, reasoning = kind, reason
            break
    if modality is None:
        if is_vision_model(model) or any(p in norm for p in MODALITY_VISION_HINTS):
            modality = "vision"
        else:
            modality = "text"

    if explicit_mod:
        modality = explicit_mod
    if explicit_reason is not None:
        reasoning = explicit_reason
    return {"modality": modality, "reasoning": bool(reasoning)}


def infer_modality(model: str) -> str:
    """自动判定模型模态能力，返回 MODALITY_CATEGORIES 的 key（保留旧签名）。"""
    return infer_capabilities(model)["modality"]


def infer_reasoning(model: str) -> bool:
    """该模型是否支持深度思考（思考链 / reasoning 模式）。"""
    return infer_capabilities(model)["reasoning"]


def is_1m_model(model: str) -> bool:
    ctx = get_context_length(model)
    return bool(ctx) and ctx >= ONE_MILLION


def mask_key(key: str) -> str:
    if not key:
        return ""
    if len(key) <= 12:
        return "****"
    return key[:6] + "****" + key[-4:]


# ============================================================
# 质量分（内存滑动窗口）
# ============================================================
def update_model_quality(key: str, info: dict):
    q = model_quality.get(key)
    if q is None:
        q = {"status_window": deque(maxlen=QUALITY_WINDOW), "latencies": deque(maxlen=QUALITY_WINDOW)}
        model_quality[key] = q
    st = info.get("status", "unknown")
    if st in ("ok", "fail", "error"):
        q["status_window"].append(st)
    if st == "ok":
        lat = info.get("latency_ms")
        if lat:
            q["latencies"].append(lat)


def get_quality_score(key: str) -> float:
    """0~1 可用率，无数据返回 1.0（乐观）"""
    q = model_quality.get(key)
    if not q or not q["status_window"]:
        return 1.0
    ok_count = sum(1 for s in q["status_window"] if s == "ok")
    return ok_count / len(q["status_window"])


def get_avg_latency(key: str):
    q = model_quality.get(key)
    if not q or not q["latencies"]:
        return None
    return sum(q["latencies"]) / len(q["latencies"])


# ============================================================
# 熔断
# ============================================================
def is_circuit_open(key: str) -> bool:
    cb = circuit_breaker.get(key)
    if not cb:
        return False
    if cb.get("open_until") and time.time() >= cb["open_until"]:
        cb["fails"] = 0
        cb["open_until"] = 0
        return False
    return bool(cb.get("open_until"))


def _provider_fault(status: int) -> bool:
    """这个状态码算不算「上游的错」。

    4xx 里除了 429 都是**请求/配置问题**（参数写错、模型名不存在、key 失效），
    把它们计入熔断，等于用户自己发了 3 个坏请求就把模型降级 60 秒。
    """
    return status >= 500 or status == 429


def record_fail(key: str):
    cb = circuit_breaker.setdefault(key, {"fails": 0, "open_until": 0})
    cb["fails"] += 1
    if cb["fails"] >= CIRCUIT_FAIL_THRESHOLD:
        cb["open_until"] = time.time() + CIRCUIT_RECOVERY_SECONDS
        logger.warning("circuit opened: %s", key)


def record_success(key: str):
    cb = circuit_breaker.get(key)
    if cb:
        cb["fails"] = 0
        cb["open_until"] = 0


# ============================================================
# 探测
# ============================================================
async def check_model(base_url: str, api_key: str, model: str) -> dict:
    actual_model = MODEL_ALIASES.get(model, model)
    url = base_url.rstrip("/") + "/chat/completions"
    payload = {
        "model": actual_model,
        "messages": [{"role": "user", "content": "hi"}],
        "max_tokens": 5,
        "stream": False,
    }
    headers = {
        **auth_headers(api_key),
        "Content-Type": "application/json",
    }
    start = time.time()
    try:
        resp = await http_client.post(url, json=payload, headers=headers, timeout=30)
        latency = round((time.time() - start) * 1000)
        if resp.status_code == 200:
            usage = resp.json().get("usage", {})
            return {
                "status": "ok",
                "code": resp.status_code,
                "latency_ms": latency,
                "prompt_tokens": usage.get("prompt_tokens", 0),
                "completion_tokens": usage.get("completion_tokens", 0),
            }
        return {
            "status": "fail",
            "code": resp.status_code,
            "latency_ms": latency,
            "detail": resp.text[:200],
        }
    except Exception as e:
        latency = round((time.time() - start) * 1000)
        return {"status": "error", "latency_ms": latency, "detail": str(e)[:200]}


def auth_headers(api_key: str | None) -> dict:
    """上游鉴权头。

    空 Key（Ollama / LM Studio 这类本机推理服务不需要鉴权）时**不发
    Authorization** —— 拼一个 "Bearer "（末尾空格）是非法 header 值，
    httpx 会直接抛 Illegal header value。免 Key 平台的 Key 就是空串，
    探测、拉模型、实际转发三条路径都得走这里，漏一处就会在运行时炸。
    """
    key = (api_key or "").strip()
    return {"Authorization": f"Bearer {key}"} if key else {}


async def fetch_model_details(base_url: str, api_key: str) -> dict:
    url = base_url.rstrip("/") + "/models"
    headers = auth_headers(api_key)
    try:
        resp = await http_client.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            model_list = data.get("data", data) if isinstance(data, dict) else data
            details = {}
            for m in model_list:
                if isinstance(m, dict) and "id" in m:
                    pricing = m.get("pricing", {})
                    details[m["id"]] = {
                        "context_length": m.get("context_length"),
                        "prompt_price": pricing.get("prompt", ""),
                        "completion_price": pricing.get("completion", ""),
                    }
            return details
    except Exception:
        logger.exception("fetch_model_details failed for %s", base_url)
    return {}


async def fetch_models_with_pricing(base_url: str, api_key: str, free_only: bool = True) -> tuple[list[str], dict[str, dict]]:
    """拉模型列表，顺带把上游自报的 pricing 一并带回来（有的话）。

    返回 (模型名列表, {模型名: {"prompt": ..., "completion": ...}})。
    pricing 只有 OpenRouter/SiliconFlow 这类"价目开放"的上游才给，其余为空 dict。
    """
    url = base_url.rstrip("/") + "/models"
    headers = auth_headers(api_key)
    pricing_map: dict[str, dict] = {}
    try:
        resp = await http_client.get(url, headers=headers, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            model_list = data.get("data", data) if isinstance(data, dict) else data
            if not isinstance(model_list, list):
                return [], pricing_map
            for m in model_list:
                if isinstance(m, dict) and "id" in m and isinstance(m.get("pricing"), dict):
                    pricing_map[m["id"]] = dict(m["pricing"])
            if free_only:
                has_pricing = any(isinstance(m, dict) and m.get("pricing") for m in model_list)
                if has_pricing:
                    free_by_api = [
                        m for m in model_list
                        if isinstance(m, dict) and "id" in m and is_free_model(m)
                    ]
                    if free_by_api:
                        return [m["id"] for m in free_by_api if is_chat_model(m["id"])], pricing_map
                free_by_name = [
                    m["id"] for m in model_list
                    if isinstance(m, dict) and "id" in m
                    and is_free_by_name(m["id"]) and is_chat_model(m["id"])
                ]
                if free_by_name:
                    return free_by_name, pricing_map
            return [
                m["id"] for m in model_list
                if "id" in m and isinstance(m, dict) and is_chat_model(m["id"])
            ], pricing_map
    except Exception:
        logger.exception("fetch_models failed for %s", base_url)
    return [], pricing_map


async def fetch_models(base_url: str, api_key: str, free_only: bool = True) -> list[str]:
    return (await fetch_models_with_pricing(base_url, api_key, free_only))[0]


async def verify_provider_key_impl(base_url: str, api_key: str) -> dict:
    """校验上游 key 是否有效：调上游 /models 接口，401/403 判定 key 无效"""
    url = base_url.rstrip("/") + "/models"
    headers = auth_headers(api_key)
    try:
        resp = await http_client.get(url, headers=headers, timeout=15)
        if resp.status_code in (401, 403):
            return {"ok": False, "detail": f"Key 无效（HTTP {resp.status_code}）"}
        if resp.status_code == 200:
            return {"ok": True, "detail": "连接成功"}
        return {"ok": False, "detail": f"上游返回 HTTP {resp.status_code}"}
    except httpx.RequestError as e:
        return {"ok": False, "detail": f"连接失败：{str(e)[:150]}"}
    except Exception as e:
        return {"ok": False, "detail": f"校验异常：{str(e)[:150]}"}


# ============================================================
# 轮询
# ============================================================
async def run_health_checks(tasks: list[tuple[str, str, str, str]]) -> dict:
    """并发检测所有 (name, model, base_url, api_key) 任务，返回 {key: result}。"""
    sem = asyncio.Semaphore(10)

    async def limited_check(url, key, m):
        async with sem:
            return await check_model(url, key, m)

    results = await asyncio.gather(
        *[limited_check(url, key, m) for _, m, url, key in tasks],
        return_exceptions=True,
    )
    new_status = {}
    for (name, m, _, _), result in zip(tasks, results, strict=True):
        k = f"{name}||{m}"
        if isinstance(result, Exception):
            new_status[k] = {
                "status": "error",
                "detail": str(result)[:200],
                "checked_at": time.time(),
            }
        else:
            result["checked_at"] = time.time()
            new_status[k] = result
        update_model_quality(k, new_status[k])
    return new_status


async def poll_all():
    global health_status, last_poll_time, last_check_time, poll_count_state, last_daily_poll_state, poll_stage
    # 先让窗口加载出来，避免卡在启动页
    poll_stage = "waiting"
    await asyncio.sleep(1.5)

    # 并发拉取所有 provider 的 model details（原来串行很慢）
    poll_stage = "fetching_models"
    async def fetch_one(p):
        try:
            return await fetch_model_details(p["base_url"], p["api_key"])
        except Exception:
            logger.exception("model_details fetch failed: %s", p.get("name"))
            return {}
    results = await asyncio.gather(*[fetch_one(p) for p in list(providers)])
    for details in results:
        if details:
            model_details.update(details)

    while True:
        try:
            # 每天中午 12 点强制检测一次（无论 poll_count 是否超过 20）
            today = time.strftime("%Y-%m-%d")
            now_hour = time.localtime().tm_hour
            should_daily_poll = (today != last_daily_poll_state and now_hour >= 12)

            # 超过 20 次且不是每日检测时间 → 跳过
            if poll_count_state >= POLL_MAX_COUNT and not should_daily_poll:
                poll_stage = "idle"
                await asyncio.sleep(30)
                continue

            poll_stage = "running"
            tasks = []
            for p in list(providers):
                for m in get_enabled_models(p):
                    tasks.append((p["name"], m, p["base_url"], p["api_key"]))
            new_status = await run_health_checks(tasks)
            health_status = new_status
            last_poll_time = time.time()
            last_check_time = last_poll_time
            await append_history(new_status)
            await maybe_cleanup_history()
            ok_count = sum(1 for v in new_status.values() if v.get("status") == "ok")
            logger.info("poll done: %d/%d ok", ok_count, len(new_status))

            # 更新轮询计数 & 每日检测标记
            poll_count_state += 1
            app_config["poll_count"] = poll_count_state
            if should_daily_poll:
                last_daily_poll_state = today
                app_config["last_daily_poll"] = today
            save_config()

        except Exception:
            logger.exception("poll_all loop error")
        # 等待到 last_check_time + POLL_INTERVAL
        while time.time() < last_check_time + POLL_INTERVAL:
            await asyncio.sleep(5)


# ============================================================
# lifespan
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global http_client, poll_task
    # 运行时日志落盘（带轮转，避免无限膨胀）；放在此处确保 uvicorn 配置 logging 后再挂，不被清空
    try:
        _fh = RotatingFileHandler(DATA_DIR / "gateway.log", maxBytes=5 * 1024 * 1024, backupCount=3, encoding="utf-8")
        _fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s [%(name)s] %(message)s"))
        logging.getLogger().addHandler(_fh)
    except Exception:
        pass
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(120.0, connect=10.0),
        limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
    )
    app.state.http = http_client
    poll_task = asyncio.create_task(poll_all())
    team_poll_task = asyncio.create_task(_team_poll_loop())
    yield
    if poll_task:
        poll_task.cancel()
    if team_poll_task:
        team_poll_task.cancel()
    await http_client.aclose()


app = FastAPI(title="模型API网关", lifespan=lifespan)
templates = Jinja2Templates(directory=str(APP_DIR / "templates"))
templates.env.auto_reload = True


@app.middleware("http")
async def no_cache_middleware(request: Request, call_next):
    """给页面和 API 响应加 no-cache，避免 pywebview/浏览器缓存旧前端。"""
    resp = await call_next(request)
    if request.url.path in ("/",) or request.url.path.startswith("/api/"):
        resp.headers["Cache-Control"] = "no-cache, no-store, must-revalidate"
        resp.headers["Pragma"] = "no-cache"
        resp.headers["Expires"] = "0"
    return resp


# ============================================================
# Pydantic 模型
# ============================================================
class ProviderIn(BaseModel):
    name: str
    base_url: str
    api_key: str
    models: list[str] = []
    # 拉模型列表时是否只保留免费模型。
    # 默认 False：混合上游（如 OpenRouter 既有 :free 又有付费模型）在 True 下
    # 会把付费模型整个过滤掉，而付费兜底正是这个网关的本职。免费与否交给
    # 下面的 free_status_override 与徽章标注，靠「隐藏」不是办法。
    # presets.json 里的 NVIDIA 等仍显式写 True，那是对具体平台的判断，不受此影响。
    free_only: bool = False
    # 免费标记人工覆盖：auto（按证据分级自动判定）| free | paid | unknown
    free_status_override: str = "auto"
    # 上游 /models 自报的价目（拉模型列表时顺带存下），是"免费"判定最权威的证据
    model_pricing: dict[str, dict] = {}


class ProviderUpdate(BaseModel):
    name: str | None = None
    base_url: str | None = None
    api_key: str | None = None
    models: list[str] | None = None
    free_only: bool | None = None
    free_status_override: str | None = None


class ToggleModelIn(BaseModel):
    model: str
    enabled: bool


class VerifyKeyIn(BaseModel):
    base_url: str
    api_key: str


class OpenUrlIn(BaseModel):
    url: str


class PresetApplyIn(BaseModel):
    keys: dict = {}
    # 默认不覆盖已配置的平台。理由：一键配置的定位是「把缺的补上」，
    # 不是「把配好的重建一遍」。团队里每人各自注册免费账号、各自手调
    # free_only 与模型禁用状态，一键配置若重建条目会把这些改动抹掉。
    # 只有用户显式勾选覆盖时才动已有的。
    overwrite_existing: bool = False


# ============================================================
# 模型选择
# ============================================================
def pick_available_models(model: str | None = None, force: bool = False) -> list[tuple[dict, str]]:
    """返回按质量排序的候选 (provider, model) 列表"""
    
    # 三层分级（学自 dsh-our-free-model 的可用性设计）：
    #   tier 0 —— 可用 / 还没探过 / **临时故障**。临时故障仍留在主池：
    #             上游半夜抽风是常态，而质量分与熔断已经在实时惩罚它了，
    #             再把它踢出候选池只会让「一个 429」变成「这个模型没了」。
    #   tier 1 —— 鉴权失败 / 不在套餐 / 地区受限 / 判不出来。模型本身还在，
    #             是调用方配置问题，改配置就能用，所以排在临时故障后面但仍可用。
    #   tier 2 —— 网关明确拒绝路由，或熔断中。这是唯一真的「别再试它」的档。
    tiers: dict = {0: [], 1: [], 2: []}

    def _tier_of(k: str) -> int:
        h = health_status.get(k)
        if is_circuit_open(k):
            return 2                        # 运行时熔断比探测判定更实时
        if not h or not h.get("status"):
            return 0                        # 还没探过 = 不算问题
        av = classify_availability(h.get("code"), h.get("status", "unknown"),
                                   h.get("detail", ""))
        if av == "unavailable":
            return 2
        if av in ("auth_failed", "plan_limited", "region_limited", "inconclusive"):
            return 1
        return 0                            # available / temporarily_broken

    # 如果请求的是自定义路由组
    if model in ROUTERS:
        target_models = set(ROUTERS[model])
        for p in providers:
            for m in get_enabled_models(p):
                if m in target_models:
                    k = f"{p['name']}||{m}"
                    # 路由组是用户显式圈定的模型，不做可用性判定 ——
                    # 显式指定就意味着「我就要它」，探测结果只影响排序不影响去留
                    tiers[0].append((p, m, k))
        scored = [
            (get_quality_score(k), get_avg_latency(k) or 1e9, p, m)
            for p, m, k in tiers[0]
        ]
        scored.sort(key=lambda x: (-x[0], x[1]))
        return [(p, m) for _, _, p, m in scored]

    # 否则按具体模型匹配
    for p in providers:
        for m in get_enabled_models(p):
            # 请求里的 model 命中本供应商的任一写法（裸名 / 供应商/模型名 / 旧供应商-模型名）
            if model and not model_id_matches(model, p["name"], m):
                continue
            k = f"{p['name']}||{m}"
            # 三层都收进来；下面按 0→1→2 取第一个非空层，
            # 所以被探测/熔断判死的模型只有在前面全空时才会被用到。
            tiers[_tier_of(k)].append((p, m, k))

    # 逐层回退，但**永不返回空列表**：只要还有一个模型，就有候选。
    # （它那套设计里也有同一条：整轮探测全部被拒时同样保留，选择器不会为空）
    for t in (0, 1, 2):
        if tiers[t]:
            picked = tiers[t]
            break
    else:
        picked = []

    scored = [
        (get_quality_score(k), get_avg_latency(k) or 1e9, p, m)
        for p, m, k in picked
    ]
    scored.sort(key=lambda x: (-x[0], x[1]))
    return [(p, m) for _, _, p, m in scored]


def pick_available_model(model: str | None = None, force: bool = False):
    cands = pick_available_models(model, force)
    return cands[0] if cands else (None, None)


# ============================================================
# Hermes 工具名压缩 / 还原
# ============================================================
HERMES_MAP = [
    ("mcp_hermes_studio_use_hermes_studio_use_", "mcp_hsu_"),
    ("mcp_hermes_studio_devices_hermes_studio_lan_", "mcp_hsd_"),
    ("mcp_hermes_studio_api_hermes_studio_api_", "mcp_hsa_"),
]


def compress_hermes(obj: dict) -> dict:
    s = json.dumps(obj, ensure_ascii=False)
    for long, short in HERMES_MAP:
        s = s.replace(long, short)
    return json.loads(s)


def restore_hermes_text(text: str) -> str:
    for long, short in HERMES_MAP:
        text = text.replace(short, long)
    return text


def merge_reasoning(obj: dict) -> dict:
    """保留 reasoning_content 字段原样透传，不合并到 content"""
    return obj

# ============================================================
# 回复语言跟随：根据用户消息语言决定回复语言
# ============================================================
LANG_HINT = (
    "\n\n【重要】请始终使用简体中文回答用户。"
    "思考过程(reasoning)也请用中文。"
    "代码、命令、文件名、专有名词、标识符等保持原样即可，不要翻译。"
)


def ensure_lang_reply(body: dict) -> dict:
    """注入简体中文回复提示。
    - 已有 system 且为纯文本：在末尾追加指令（带判重，幂等）。
    - 无 system：在最前面插入一条 system。"""
    msgs = body.get("messages")
    if not isinstance(msgs, list) or not msgs:
        return body
    first = msgs[0]
    if isinstance(first, dict) and first.get("role") == "system":
        c = first.get("content")
        if isinstance(c, str) and "请始终使用简体中文" not in c:
            first["content"] = c.rstrip() + LANG_HINT
        return body
    msgs.insert(0, {"role": "system", "content": "请使用简体中文回答。" + LANG_HINT})
    return body


# ============================================================
# 页面
# ============================================================
@app.get("/", response_class=HTMLResponse)
async def index(request: Request):
    ctx = {
        "request": request,
        "local_api_key": LOCAL_API_KEY,
        "app_version": APP_VERSION,
    }
    return templates.TemplateResponse(request, "index.html", ctx)


# ==== 组队（team）API ====
# 客户端逻辑全在 team_client.py，这里只做 HTTP 转发 + 与本地 providers 联动。
# 安全边界见 team_client.py 顶部注释：密钥一旦复制到队员机器就无法真正收回。
import team_client as _tc

team_state = _tc.TeamState(DATA_DIR)


@app.get("/api/team/state")
async def team_state_info(_=Depends(verify_admin)):
    """本地组队状态概览（含每队成员、本地队伍来的 provider 统计）。"""
    teams = []
    for tid, t in team_state.list_teams().items():
        remote = await _tc.fetch_team_state(team_state, tid)
        teams.append({
            "team_id": tid,
            "role": t.get("role"),
            "status": t.get("status", "active"),
            "nickname": t.get("nickname", ""),
            # 码只给队长自己看（队员也需要它派生密钥，但已经在本地）
            "code": t.get("code", "") if t.get("role") == "captain" else "",
            "code_hint": ("•" * 6) if t.get("role") != "captain" else t.get("code", ""),
            "expires_at": t.get("expires_at"),
            "alive": bool(remote.get("alive")) if remote.get("ok") else False,
            "remote_status": remote.get("status"),
            "members": remote.get("members", []),
            "me": remote.get("me", {}),
            "error": remote.get("error") if not remote.get("ok") else None,
        })
    summary = _tc.team_providers_summary(providers)
    return {
        "ok": True,
        "client_id": team_state.client_id,
        "server": team_state.server,
        "teams": teams,
        "team_providers": summary,
    }


@app.post("/api/team/create")
async def team_create(data: dict, _=Depends(verify_admin)):
    nickname = str(data.get("nickname") or "队长")[:32]
    # 不能写成 `data.get("ttl") or 7*86400`：ttl=0 是「永久」的合法取值，
    # 而 0 是 falsy，会被当成没传从而落回默认 7 天 —— 永久队伍建不出来。
    raw_ttl = data.get("ttl")
    ttl = 7 * 86400 if raw_ttl is None else int(raw_ttl)
    code_len = 8 if data.get("code_len") == 8 else 6
    return await _tc.create_team(team_state, nickname, ttl, code_len)


@app.post("/api/team/join")
async def team_join(data: dict, _=Depends(verify_admin)):
    team_id = str(data.get("team_id") or "").strip()
    code = str(data.get("code") or "").strip()
    nickname = str(data.get("nickname") or "队员")[:32]
    if not team_id or not code:
        return {"ok": False, "error": "队伍 ID 和组队码都不能为空"}
    return await _tc.join_team(team_state, team_id, code, nickname)


@app.post("/api/team/approve")
async def team_approve(data: dict, _=Depends(verify_admin)):
    return await _tc.approve_member(team_state, str(data.get("team_id") or ""),
                                    str(data.get("client_id") or ""))


@app.post("/api/team/kick")
async def team_kick(data: dict, _=Depends(verify_admin)):
    tid = str(data.get("team_id") or "")
    cid = str(data.get("client_id") or "")
    res = await _tc.kick_member(team_state, tid, cid)
    return res


@app.post("/api/team/nickname")
async def team_nickname(data: dict, _=Depends(verify_admin)):
    return await _tc.set_nickname(team_state, str(data.get("team_id") or ""),
                                  str(data.get("nickname") or ""))


@app.post("/api/team/leave")
async def team_leave(data: dict, _=Depends(verify_admin)):
    """本地离开队伍。队伍带来的 provider 会被置为失效（不删除，用户可自行删）。"""
    tid = str(data.get("team_id") or "")
    res = await _tc.leave_team(team_state, tid)
    if res.get("ok"):
        async with providers_lock:
            providers[:] , n = _tc.mark_team_dead(providers, tid, "left")
            save_providers(providers)
        res["marked_dead"] = n
    return res


@app.post("/api/team/close")
async def team_close(data: dict, _=Depends(verify_admin)):
    tid = str(data.get("team_id") or "")
    return await _tc.close_team(team_state, tid)


@app.post("/api/team/sync")
async def team_sync(data: dict, _=Depends(verify_admin)):
    """队长把指定上游（含 api_key）加密推给指定队员。

    这是**唯一的密钥外流通道**。界面上必须已警告过「不可收回」。
    """
    tid = str(data.get("team_id") or "")
    cid = str(data.get("client_id") or "")
    names = data.get("providers") or []
    if isinstance(names, str):
        names = [names]
    t = team_state.get_team(tid)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "只有队长能同步上游"}
    if not cid:
        return {"ok": False, "error": "未指定队员"}
    by_name = {p.get("name"): p for p in providers}
    results = []
    for n in names:
        p = by_name.get(n)
        if not p:
            results.append({"name": n, "ok": False, "error": "本地无此上游"})
            continue
        r = await _tc.sync_provider_to_member(team_state, tid, cid, p)
        results.append({"name": n, "ok": bool(r.get("ok")), "error": r.get("error")})
    ok = sum(1 for r in results if r.get("ok"))
    return {"ok": ok > 0, "sent": ok, "total": len(names), "results": results,
            "warning": "密钥已复制到对方机器，无法远程收回。撤销只能靠对方删除或你自己换 key。"}


@app.get("/api/team/sync-allow")
async def team_sync_allow_get(_=Depends(verify_admin)):
    """白名单：哪些本地上游允许同步给全队。

    **默认全开** —— 用户定的口径是「默认都是同步的，只有队长显式关掉
    才不同步」。没写到的平台一律视为允许。
    """
    out = {}
    for tid in team_state.list_teams():
        t = team_state.get_team(tid)
        if t and t.get("role") == "captain":
            out[tid] = t.get("sync_allow") or {}
    return {"ok": True, "allow": out,
            "providers": [{"name": p.get("name"),
                           "from_team": bool(_tc._provider_team_id(p))}
                          for p in providers]}


@app.post("/api/team/sync-allow")
async def team_sync_allow_set(data: dict, _=Depends(verify_admin)):
    """队长改白名单。只有队长能改 —— 这是密钥外流的闸门。"""
    tid = str(data.get("team_id") or "")
    allow = data.get("allow") or {}
    t = team_state.get_team(tid)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "只有队长能调整同步范围"}
    if not isinstance(allow, dict):
        return {"ok": False, "error": "allow 必须是对象"}
    t["sync_allow"] = {str(k): bool(v) for k, v in allow.items()}
    team_state.put_team(tid, t)
    off = [k for k, v in t["sync_allow"].items() if not v]
    return {"ok": True, "allow": t["sync_allow"], "disabled": off,
            "warning": ("密钥一旦同步就是复制到对方机器，无法远程收回；"
                        "关掉只是不再推新的，已经推过去的收不回来。") if off else ""}


@app.post("/api/team/rotate-code")
async def team_rotate_code(data: dict, _=Depends(verify_admin)):
    """轮换入队码。永久队伍尤其需要：码泄漏等于永久开放。"""
    tid = str(data.get("team_id") or "")
    code_len = 8 if data.get("code_len") == 8 else 6
    t = team_state.get_team(tid)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "只有队长能轮换组队码"}
    return await _tc.rotate_code(team_state, tid, code_len)


@app.post("/api/team/pull")
async def team_pull(data: dict, _=Depends(verify_admin)):
    """队员拉本队密文包并解密导入本地。关队/被踢的队伍会被置失效。"""
    tid = str(data.get("team_id") or "")
    tids = [tid] if tid else list(team_state.list_teams().keys())
    imported, notes, dead_total = [], [], 0
    for one in tids:
        t = team_state.get_team(one)
        if not t:
            continue
        # 先看队伍是否还活着，死了就把相关 provider 置失效
        remote = await _tc.fetch_team_state(team_state, one)
        if remote.get("ok"):
            me_status = (remote.get("me") or {}).get("status")
            if not remote.get("alive") or me_status == "kicked":
                async with providers_lock:
                    reason = "kicked" if me_status == "kicked" else "dissolved"
                    providers[:], n = _tc.mark_team_dead(providers, one, reason)
                    if n:
                        save_providers(providers)
                    dead_total += n
                if me_status == "kicked":
                    t["status"] = "kicked"
                    team_state.put_team(one, t)
                elif not remote.get("alive"):
                    t["status"] = "closed"
                    team_state.put_team(one, t)
                continue
            if me_status == "approved" and t.get("status") != "approved":
                t["status"] = "approved"
                team_state.put_team(one, t)

        provs = await _tc.pull_packages(team_state, one)
        if not provs:
            continue
        # 队长昵称从远端成员表取（本地 t.nickname 是我自己的昵称）
        captain_name = ""
        if remote.get("ok"):
            for m in remote.get("members", []):
                if m.get("role") == "captain":
                    captain_name = m.get("nickname") or ""
                    break
        async with providers_lock:
            for p in provs:
                providers[:], added, note = _tc.merge_team_provider(
                    providers, p, one, captain_name)
                if added:
                    notes.append(note)
                    imported.append(p.get("name"))
            if notes:
                save_providers(providers)
    return {"ok": True, "imported": imported, "notes": notes, "marked_dead": dead_total}


@app.post("/api/team/report-usage")
async def team_report_usage(_=Depends(verify_admin)):
    return await _tc.report_usage(team_state, DATA_DIR)


@app.get("/api/team/usage")
async def team_usage(team_id: str, _=Depends(verify_admin)):
    return await _tc.fetch_usage_stats(team_state, team_id)


@app.get("/api/team/available-models")
async def team_available_models(team_id: str, _=Depends(verify_admin)):
    """队长视角：该队每个成员能拿到哪些上游（给「允许同步」开关用）。"""
    t = team_state.get_team(team_id)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "只有队长能看这个"}
    remote = await _tc.fetch_team_state(team_state, team_id)
    return {"ok": True, "members": remote.get("members", []),
            "providers": [{"name": p.get("name"), "base_url": p.get("base_url"),
                           "models": len(p.get("models") or []),
                           "from_team": bool(p.get(_tc.TEAM_ORIGIN_KEY))}
                          for p in providers]}


@app.post("/api/team/set-server")
async def team_set_server(data: dict, _=Depends(verify_admin)):
    url = str(data.get("url") or "").strip().rstrip("/")
    if not url.startswith(("http://", "https://")):
        return {"ok": False, "error": "地址必须以 http:// 或 https:// 开头"}
    team_state.set_server(url)
    return {"ok": True, "server": team_state.server}


# ---- 后台轮询：定时拉包、检测队伍状态、上报用量 ----
async def _team_poll_loop():
    """每 45 秒一轮：拉密文包（含置失效检测）+ 上报用量。失败静默，不影响主服务。"""
    await asyncio.sleep(8)                      # 等主服务先起来
    while True:
        try:
            if team_state.list_teams():
                await _tc.pull_packages_on_all(team_state, providers, save_providers, providers_lock)
                await _tc.report_usage(team_state, DATA_DIR)
        except Exception:
            logger.exception("team poll failed")
        await asyncio.sleep(45)


# ============================================================
# 管理接口（admin 鉴权）
# ============================================================
_page_diag_seen: dict = {}


def page_diag_dedup(text: str, now: float) -> bool:
    """True=允许记录。同内容 60s 内只记一次——防 JS 错误风暴刷爆 gateway.log。
    超 256 条直接清空（都是 60s 内的热键，清了最多多记几条，无碍）。"""
    import hashlib
    key = hashlib.md5(text.encode("utf-8", "replace")).hexdigest()
    last = _page_diag_seen.get(key)
    if last is not None and now - last < 60.0:
        return False
    _page_diag_seen[key] = now
    if len(_page_diag_seen) > 256:
        _page_diag_seen.clear()
    return True


@app.post("/api/activate")
def api_activate():
    """（同机单实例）第二实例启动时调用：把本实例窗口带到前台。
    与 page-diag 同级 localhost 无鉴权——本机同用户进程即可调用，不泄露数据。"""
    return {"ok": present_window_best_effort()}


@app.post("/api/page-diag")
async def page_diag(request: Request):
    """前端自诊断回传：记录 UI 状态/JS 错误到日志（仅 localhost，≤4KB，无敏感数据）。
    窗口内的 JS 问题（按钮点了没反应等）靠它定位——共享 localStorage 会被探针覆盖，不可靠。"""
    try:
        cl = request.headers.get("content-length")
        if cl and cl.isdigit() and int(cl) > 8192:
            return {"ok": False, "err": "too large"}  # 超限不读 body
        body = (await request.body())[:4096].decode("utf-8", "replace").replace("\n", " ")
        if body and page_diag_dedup(body, time.time()):
            logger.info("PAGE-DIAG %s", body)
    except Exception:
        pass
    return {"ok": True}


@app.get("/api/poll-status")
async def poll_status(_=Depends(verify_admin)):
    return {
        "last_poll_time": last_poll_time,
        "total_models": sum(len(get_enabled_models(p)) for p in providers),
        "poll_count": poll_count_state,
        "poll_max": POLL_MAX_COUNT,
        "stage": poll_stage,
    }


@app.get("/api/history")
async def get_history(hours: int = 24, _=Depends(verify_admin)):
    return await read_history(hours)


_stability_cache: dict = {}
STABILITY_CACHE_TTL = 30


def classify_probe_failure(code, status, detail: str = "") -> str:
    """把一次失败的巡检归类成给人看的一行原因（监控页用）"""
    d = (detail or "").lower()
    if status == "error" or code is None:
        if "timeout" in d or "timed out" in d:
            return "探测超时"
        return "探测异常"
    if code == 400:
        return "请求被拒（不支持该调用）"
    if code == 401:
        return "鉴权失败"
    if code == 403:
        return "无权限 / 不在套餐"
    if code == 404:
        return "上游无此模型"
    if code == 410:
        return "模型已下线"
    if code == 429:
        return "触发限流"
    if 500 <= code < 600:
        return "上游故障"
    return f"HTTP {code}"


# 模型可用性的判定值域。与「给人看的原因」分开：那个只描述现象，
# 这个决定行为（要不要继续留在候选池、要不要从列表里移除）。
AVAIL_STATES = {
    "available": "可用",
    "unprobed": "未探测",
    "unavailable": "模型不可用",
    "region_limited": "地区受限",
    "temporarily_broken": "临时故障",
    "auth_failed": "鉴权失败",
    "plan_limited": "不在套餐",
    "inconclusive": "未能判定",
}

# 只有「网关明确拒绝路由」的证据才算模型不可用。
# 判据是响应体里的原话，比 HTTP 码更可靠 —— 有些网关对不存在的模型返回 400/500。
_UNROUTABLE_MARKERS = (
    "model is unavailable", "model not found", "model_not_found",
    "does not exist", "no such model", "unknown model",
    "invalid model", "model not supported", "unsupported model",
)
_REGION_MARKERS = (
    "region", "country", "territory", "not available in",
    "unsupported country", "地区", "国家", "区域",
)


def classify_availability(code, status: str, detail: str = "") -> str:
    """把一次探测结果归类成**决定行为**的判定（与 classify_probe_failure 分工）。

    classify_probe_failure 只负责给人看一行原因；这个负责决定模型要不要继续
    留在候选池。两者一起返回，互不替代。

    分层原则（学自 dsh-our-free-model 的可用性设计）：

    1. **只有「网关明确拒绝路由」才算模型不可用** —— 404/410，或响应体里出现
       `Model is unavailable` / `model not found` 之类的原话。这类才该从列表移除。
    2. **临时故障（5xx / 429 / 408 / 超时 / 断网）不属于对模型的判定** ——
       一律保持可达。上游半夜抽风不该让模型从下拉框里消失。
    3. **鉴权失败与套餐问题不是模型问题** —— 401 是 key 配错了，403 是没买那个档，
       模型本身仍在那儿，改配置就能用。
    4. 判不出来就是判不出来，返回 inconclusive，别硬猜。
    """
    if status == "ok":
        return "available"
    if status == "error":                     # 网络层：超时、断网、DNS、连接拒绝
        return "temporarily_broken"
    d = (detail or "").lower()

    # ① 明确拒绝路由 → 模型不可用（优先于码判定，因为有些网关用 400/500 表达）
    if any(mk in d for mk in _UNROUTABLE_MARKERS):
        return "unavailable"
    if code in (404, 410):
        return "unavailable"

    # ② 地区限制：403 的一个子类，但要看到地区相关字样才算
    if code == 403 and any(mk in d for mk in _REGION_MARKERS):
        return "region_limited"

    # ③ 临时故障：限流、请求超时、上游故障
    if code == 429 or code == 408:
        return "temporarily_broken"
    if code is not None and 500 <= code < 600:
        return "temporarily_broken"

    # ④ 鉴权 / 套餐 / 参数：模型本身没问题，是调用方配置
    if code == 401:
        return "auth_failed"
    if code == 403:
        return "plan_limited"
    return "inconclusive"


def build_stability_rows(model_stats: dict, allowed, health: dict) -> list:
    """把巡检统计拼成监控页「模型可用性」表格的行。

    只要在提供商里声明过就出一行。checks=0 表示本时间窗口内还没巡检到，可用率按 None 处理
    （页面显示 “--”）。以前这里会把「探过但一次都没成功」的模型整行删掉，后果是：
      · 「全部状态」筛选名不副实（112 个模型只列 55 个）；
      · 「服务异常（<50%）」这一档永远筛不到真正 0% 的模型 —— 恰恰是最该被看见的那批。
    因此不再隐藏，改为照常返回，只在排序时沉底。"""
    rows = []
    for key in allowed:
        s = model_stats.get(key) or {"ok": 0, "fail": 0, "error": 0, "total": 0, "latencies": []}
        h = health.get(key) or {}
        name, model = key.split("||", 1)
        lats = s["latencies"]
        avg_lat = sum(lats) / len(lats) if lats else None
        rows.append({
            "provider": name,
            "model": model,
            "checks": s["total"],
            "ok": s["ok"],
            "fail": s["fail"],
            "error": s["error"],
            "availability": round(s["ok"] / s["total"] * 100, 1) if s["total"] else None,
            "avg_latency_ms": round(avg_lat) if avg_lat else None,
            "min_latency_ms": min(lats) if lats else None,
            "max_latency_ms": max(lats) if lats else None,
            "last_status": h.get("status", "unknown"),
            "last_code": h.get("code"),
            "last_reason": None if h.get("status") == "ok" else (
                classify_probe_failure(h.get("code"), h.get("status", "unknown"), h.get("detail", ""))
                if h else None),
            # 判定层：决定这个模型要不要继续留在候选池（与 last_reason 分工，
            # 那个给人看、这个给路由用）。
            # 「还没探过」与「探过但判不出来」是两回事，必须分开 ——
            # 前者只是还没轮到，后者是证据不足。混在一起会让用户误以为
            # 一个从没探过的模型有问题。
            # 注意别叫 availability —— 那个名字被「可用率百分比」占了。
            "availability_state": (classify_availability(
                h.get("code"), h.get("status", "unknown"), h.get("detail", ""))
                if h and h.get("status") else "unprobed"),
            "vision": is_vision_model(model),
        })
    # 可用率高的在前；从未成功的（0%）沉到倒数第二档；本窗口还没巡检过的（None）垫底
    rows.sort(key=lambda r: (-r["availability"] if r["availability"] is not None else 1,
                             r["avg_latency_ms"] or 99999))
    return rows


@app.get("/api/stability")
async def get_stability(hours: int = 24, _=Depends(verify_admin)):
    now = time.time()
    cached = _stability_cache.get(hours)
    if cached and now - cached[0] < STABILITY_CACHE_TTL:
        return cached[1]
    records = await read_history(hours)
    model_stats: dict = {}
    last_probe: dict = {}          # 每条记录里的当次探测结果；文件顺序＝时间顺序，后写覆盖前写
    for rec in records:
        for key, info in rec.get("data", {}).items():
            last_probe[key] = info
            if key not in model_stats:
                model_stats[key] = {"ok": 0, "fail": 0, "error": 0, "total": 0, "latencies": []}
            model_stats[key]["total"] += 1
            st = info.get("status", "unknown")
            if st == "ok":
                model_stats[key]["ok"] += 1
                if info.get("latency_ms"):
                    model_stats[key]["latencies"].append(info["latency_ms"])
            elif st == "fail":
                model_stats[key]["fail"] += 1
            elif st == "error":
                model_stats[key]["error"] += 1
    # 去重后按提供商声明顺序出全部行（含从没成功过的，见 build_stability_rows 说明）
    allowed = list(dict.fromkeys(f"{p['name']}||{m}" for p in providers for m in p.get("models", [])))
    # 内存里的实时状态比历史记录新，优先用它；应用刚起来还没巡检时用历史记录兜底，
    # 否则「失败原因」会空一片。
    merged_probe = {**last_probe, **{k: v for k, v in health_status.items() if v}}
    result = build_stability_rows(model_stats, allowed, merged_probe)
    _stability_cache[hours] = (now, result)
    return result


@app.get("/api/usage")
async def get_usage(days: int = 1, _=Depends(verify_admin)):
    # days<=0 = "全部"（有史以来）；>0 才钳到 1..MAX_USAGE_DAYS
    days = 0 if days <= 0 else max(1, min(days, MAX_USAGE_DAYS))
    records = await read_usage(days)
    total = {"pt": 0, "ct": 0, "tt": 0, "requests": 0}
    by_day = {}
    by_model = {}
    for r in records:
        ts = r.get("ts", 0)
        day = time.strftime("%Y-%m-%d", time.localtime(ts))
        pt = r.get("pt", 0) or 0
        ct = r.get("ct", 0) or 0
        tt = r.get("tt", 0) or (pt + ct)
        m = r.get("model", "unknown")
        p = r.get("provider", "unknown")
        total["pt"] += pt
        total["ct"] += ct
        total["tt"] += tt
        total["requests"] += 1
        d = by_day.setdefault(day, {"pt": 0, "ct": 0, "tt": 0, "requests": 0})
        d["pt"] += pt
        d["ct"] += ct
        d["tt"] += tt
        d["requests"] += 1
        mk = local_model_id(p, m)
        mm = by_model.setdefault(mk, {"pt": 0, "ct": 0, "tt": 0, "requests": 0, "provider": p, "model": m})
        mm["pt"] += pt
        mm["ct"] += ct
        mm["tt"] += tt
        mm["requests"] += 1
    by_day_list = [{"date": d, **v} for d, v in sorted(by_day.items())]
    by_model_list = [
        {"provider": v["provider"], "model": v["model"], "pt": v["pt"], "ct": v["ct"], "tt": v["tt"], "requests": v["requests"]}
        for _, v in sorted(by_model.items(), key=lambda x: -x[1]["tt"])
    ]
    return {"days": days, "total": total, "by_day": by_day_list, "by_model": by_model_list}


@app.get("/api/model-details")
async def get_model_details(_=Depends(verify_admin)):
    merged = {}
    # 1. 上游探测结果（键为上游模型 id / 原始名）
    for k, v in model_details.items():
        merged[k] = dict(v)
    # 2. 对 providers 里每个模型(原始名)，用别名归一化查 meta 兜底
    #    解决魔搭等 provider 用别名形式(如 ZhipuAI/GLM-5.2)而 meta 里
    #    只有规范化名(如 glm-5.2) 导致前端查不到上下文/描述的问题

    for p in providers:
        for m in p.get("models", []):
            entry = merged.setdefault(m, {})
            norm = MODEL_ALIASES.get(m, m)
            meta_desc = MODEL_DESCRIPTIONS.get(norm, {})
            if not entry.get("context_length"):
                ctx = meta_desc.get("ctx") or CONTEXT_LIMITS.get(norm)
                if ctx:
                    entry["context_length"] = ctx
            if not entry.get("desc"):
                desc = meta_desc.get("desc", "")
                if desc:
                    entry["desc"] = desc
    # 3. 对 meta 里规范化名也建条目（兼容以规范化名查询）
    for k, v in MODEL_DESCRIPTIONS.items():
        if k not in merged:
            merged[k] = {}
        # 上游 context_length 为 None/0/缺失时，用元数据覆盖
        if not merged[k].get("context_length"):
            merged[k]["context_length"] = v.get("ctx")
        merged[k]["desc"] = v.get("desc", "")
    return merged


@app.get("/api/context-limits")
async def get_context_limits(_=Depends(verify_admin)):
    return {"ok": True, "data": CONTEXT_LIMITS}

class ContextLimitUpdate(BaseModel):
    model: str
    context_length: int

@app.put("/api/context-limits")
async def update_context_limit(req: ContextLimitUpdate, _=Depends(verify_admin)):
    global CONTEXT_LIMITS, meta
    meta = load_meta()
    if "context_limits" not in meta:
        meta["context_limits"] = {}
    meta["context_limits"][req.model] = req.context_length
    CONTEXT_LIMITS[req.model] = req.context_length
    atomic_write(META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.delete("/api/context-limits/{model}")
async def delete_context_limit(model: str, _=Depends(verify_admin)):
    """删除某条自定义上下文长度配置"""
    global CONTEXT_LIMITS, meta
    meta = load_meta()
    if "context_limits" in meta and model in meta["context_limits"]:
        del meta["context_limits"][model]
    CONTEXT_LIMITS.pop(model, None)
    atomic_write(META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
    return {"ok": True}


@app.get("/api/routers")
async def get_routers_api(_=Depends(verify_admin)):
    return {"ok": True, "data": ROUTERS}


class CapabilityUpdate(BaseModel):
    model: str
    modality: str = "auto"            # 8 个能力 key 之一；"auto" 表示恢复自动判定
    reasoning: Optional[bool] = None  # None 表示恢复自动判定


@app.post("/api/model-capability")
async def update_model_capability(req: CapabilityUpdate, _=Depends(verify_admin)):
    """人工纠正某模型的能力判定，落盘 models_meta.json 的 modalities 表并即时生效。

    只写入显式给出的维度：modality="auto" 或 reasoning=None 表示该维度回落自动判定；
    两者都不显式给出时删除整条覆盖。下次 /v1/models 即返回纠正后的结果。
    """
    global MODEL_MODALITIES, meta
    if req.modality != "auto" and req.modality not in MODALITY_CATEGORIES:
        return {"ok": False, "error": "未知的能力类别"}
    key = normalize_model(req.model)
    meta = load_meta()
    table = meta.setdefault("modalities", {})
    entry = {}
    if req.modality != "auto":
        entry["modality"] = req.modality
    if req.reasoning is not None:
        entry["reasoning"] = bool(req.reasoning)
    if entry:
        table[key] = entry
    else:
        table.pop(key, None)
    MODEL_MODALITIES = table
    atomic_write(META_FILE, json.dumps(meta, indent=2, ensure_ascii=False))
    _models_cache["data"] = None      # 立即失效，下次拉取重算
    return {"ok": True, "data": infer_capabilities(req.model)}

@app.post("/api/routers")
async def save_routers_api(request: Request, _=Depends(verify_admin)):
    global ROUTERS
    body = await request.json()
    ROUTERS = body
    save_routers()
    return {"ok": True}


@app.get("/api/vision-models")
async def vision_models_api(_=Depends(verify_admin)):
    """返回 supports_vision 标记的模型名列表，供前端识图配置标记"""
    return {"ok": True, "data": sorted(SUPPORTS_VISION.keys())}


# ---------- 系统公告（远端拉取，本地兜底） ----------
# 公告源指向本项目自己的仓库（默认分支 revive 上的 announcement.md）。
# 想改公告不必改代码：改仓库里那个文件就行，客户端最长 5 分钟后生效；
# 断网 / 首次启动 / 远端抓取失败时用下面的 FALLBACK_ANNOUNCEMENT 兜底。
DEFAULT_ANNOUNCEMENT_URL = "https://raw.githubusercontent.com/chenshifanjian/model-reservoir/revive/announcement.md"
FALLBACK_ANNOUNCEMENT = """# 模型蓄水池 · Model Reservoir

欢迎使用。本软件把多个 LLM 提供商的额度聚合成一个 **OpenAI 兼容接口**，供本机各种客户端调用。

- 当前版本：**v{version}**
- 开源仓库：https://github.com/chenshifanjian/model-reservoir

> 本仓库是 [zk-2025/model-gateway](https://github.com/zk-2025/model-gateway)（作者：东野 / ywtc000）的二创分支，
> 已获原作者书面授权，遵循 **CC BY-NC 4.0**：保留署名，禁止商用。

配置入口在「上游」页，调用情况看「用量」与「调用记录」两页。
""".replace("{version}", APP_VERSION)
ANNOUNCEMENT_CACHE_FILE = DATA_DIR / "announcement_cache.json"
_announcement_cache = {"content": None, "ts": 0}
ANNOUNCEMENT_TTL = 300


def _content_hash(content: str) -> str:
    """计算内容 MD5 指纹，用于前端检测公告变动"""
    return hashlib.md5(content.encode("utf-8")).hexdigest()


def _announce_response(ok: bool, content: str) -> dict:
    return {"ok": ok, "content": content, "hash": _content_hash(content)}


@app.get("/api/announcement")
async def get_announcement(_=Depends(verify_admin)):
    """优先按 config.json 的 announcement_url（任意 http/https raw 链接）远程抓取；
    未配置或抓取失败时依次回退：上次成功的缓存 → 本地 announcement.json → 内置兜底文案。
    远端结果缓存 5 分钟。"""
    cfg = load_config()
    url = cfg.get("announcement_url") or DEFAULT_ANNOUNCEMENT_URL
    now = time.time()
    if _announcement_cache["content"] is not None and now - _announcement_cache["ts"] < ANNOUNCEMENT_TTL:
        return _announce_response(True, _announcement_cache["content"])
    # 远程抓取
    try:
        resp = await http_client.get(url, timeout=10, follow_redirects=True)
        if resp.status_code == 200 and resp.text.strip():
            content = resp.text
            _announcement_cache["content"] = content
            _announcement_cache["ts"] = now
            # 持久化到本地缓存文件，断网时回退显示上次成功的内容
            try:
                atomic_write(ANNOUNCEMENT_CACHE_FILE, json.dumps({"content": content, "ts": now}, ensure_ascii=False))
            except Exception:
                logger.warning("write announcement cache file failed")
            return _announce_response(True, content)
    except Exception:
        logger.warning("fetch remote announcement failed: %s", url)
    # 远程失败：读本地缓存文件（上次成功抓取的内容）
    if ANNOUNCEMENT_CACHE_FILE.exists():
        try:
            data = json.loads(ANNOUNCEMENT_CACHE_FILE.read_text(encoding="utf-8"))
            if data.get("content"):
                return _announce_response(True, data["content"])
        except Exception:
            pass
    # 最终兜底：默认 announcement.json
    if ANNOUNCEMENT_FILE.exists():
        try:
            data = json.loads(ANNOUNCEMENT_FILE.read_text(encoding="utf-8"))
            return _announce_response(True, data.get("content", ""))
        except Exception:
            logger.exception("parse announcement.json failed")
    # 内置兜底文案：ok 仍为 True——对使用者来说这就是"有内容"，
    # 让前端照常按 Markdown 渲染，而不是掉进 ok=False 的纯文本分支
    return _announce_response(True, FALLBACK_ANNOUNCEMENT)


# ---------- 在线更新 ----------
VERSION_CHECK_URL = "https://raw.githubusercontent.com/chenshifanjian/model-reservoir/revive/version.json"
RELEASE_PAGE_URL = "https://github.com/chenshifanjian/model-reservoir/releases"


def platform_key() -> str:
    """当前平台标识：windows / macos / linux"""
    if sys.platform.startswith("win"):
        return "windows"
    if sys.platform == "darwin":
        return "macos"
    return "linux"


def download_url_for_platform(data: dict) -> str:
    """version.json 允许按平台给不同产物：download_url_<平台> 优先，其次通用 download_url。
    一个 version.json 服务三端，免得 Linux/macOS 用户下到 Windows 的 exe。"""
    return data.get("download_url_" + platform_key()) or data.get("download_url", "")


def hot_update_supported() -> bool:
    """程序内热替换二进制只做了 Windows：其它平台只能手动换包"""
    return sys.platform == "win32" and getattr(sys, "frozen", False)
_update_download_state = {
    "downloading": False,
    "progress": 0,
    "total": 0,
    "done": False,
    "error": None,
    "file": None,
}


def _assert_download_ok(actual_sha256: str, actual_size: int,
                        expect_sha256: str, expect_size: int) -> None:
    """校验下载产物是否与 version.json 声明的一致。不一致抛 ValueError。

    抽成纯函数是为了能直接测「篡改后必须拒绝」，不用起 HTTP 服务。
    两项都为 0/空时放行（老版 version.json 没有这些字段），但要留日志。
    """
    if expect_size and actual_size != expect_size:
        raise ValueError(
            f"大小不符：期望 {expect_size} 字节，实际 {actual_size} 字节"
            f"（下载不完整，或服务器返回的不是完整文件）")
    if expect_sha256 and actual_sha256.lower() != expect_sha256.lower():
        raise ValueError(
            f"SHA256 不符：期望 {expect_sha256[:16]}…，实际 {actual_sha256[:16]}…"
            f"（文件已损坏，或下载来源被替换）")


def _looks_like_executable(path: str) -> bool:
    """按文件头粗筛「这是不是本平台的可执行文件」。

    主防线是 sha256；这个兜底防的是「下到一个 HTML 错误页 / 下了一半」，
    那种文件就算没有 sha256 也不该被换上去。
    """
    try:
        with open(path, "rb") as f:
            head = f.read(4)
    except Exception:
        return False
    if len(head) < 2:
        return False
    if sys.platform == "win32":
        return head[:2] == b"MZ"
    if sys.platform == "darwin":
        return head[:4] in (b"\xcf\xfa\xed\xfe", b"\xce\xfa\xed\xfe",
                            b"\xca\xfe\xba\xbe")
    return head[:4] == b"\x7fELF"


_VERSION_RE = re.compile(r"^[vV]?(\d+(?:\.\d+)*)")


def _parse_version(v):
    """把 "v1.9.2" / "1.9.2-beta" 解析成 [1,9,2]；空串、None、垃圾返回 None。

    宽松是有意的：预发布后缀（-beta/-rc1）按主版本比较，够用且符合直觉；
    但**解析不出来时绝不能再退化成「字符串不相等就是新版」** —— 那会让一个
    空的 / 损坏的 version.json 变成每次启动都弹「发现新版本」。
    """
    if not isinstance(v, str):
        return None
    m = _VERSION_RE.match(v.strip())
    if not m:
        return None
    return [int(x) for x in m.group(1).split(".")]


def _version_gt(a, b) -> bool:
    """a 严格新于 b 才返回 True。任一侧解析失败一律 False（不提示更新）。"""
    pa, pb = _parse_version(a), _parse_version(b)
    if pa is None or pb is None:
        return False
    n = max(len(pa), len(pb))
    pa += [0] * (n - len(pa))
    pb += [0] * (n - len(pb))
    return pa > pb


@app.get("/api/check-update")
async def check_update(_=Depends(verify_admin)):
    """检查远端 version.json 是否有新版本（下载地址按当前平台挑）"""
    cfg = load_config()
    url = cfg.get("version_check_url") or VERSION_CHECK_URL
    try:
        resp = await http_client.get(url, timeout=10, follow_redirects=True)
        if resp.status_code == 200:
            data = resp.json()
            if not isinstance(data, dict):
                return {"ok": False, "error": "版本信息格式错误"}
            latest_ver = data.get("version", "")
            has_update = _version_gt(latest_ver, APP_VERSION)
            min_ver = data.get("min_version", "")
            force_update = bool(min_ver and _version_gt(min_ver, APP_VERSION))
            # 按平台取产物元信息；老版 version.json 没有 artifacts 就返回空，
            # 客户端会退化成「不校验」并留告警，不会因为字段缺失就拒绝更新。
            art = (data.get("artifacts") or {}).get(platform_key()) or {}
            if not isinstance(art, dict):
                art = {}
            return {
                "ok": True,
                "current": APP_VERSION,
                "latest": latest_ver,
                "has_update": has_update,
                "force_update": force_update,
                "download_url": download_url_for_platform(data),
                "release_page": data.get("release_page") or RELEASE_PAGE_URL,
                "release_notes": data.get("release_notes", ""),
                "min_version": min_ver,
                "platform": platform_key(),
                "hot_update": hot_update_supported(),
                "sha256": (art.get("sha256") or "").lower(),
                "size": int(art.get("size") or 0),
            }
    except Exception as e:
        logger.warning("check update failed: %s", e)
    return {"ok": False, "error": "无法连接更新服务器"}


@app.post("/api/start-download")
async def start_download(data: dict, _=Depends(verify_admin)):
    """启动后台下载新版 exe，返回后通过 /api/download-progress 轮询进度。

    sha256 / size 由前端从 /api/check-update 带回 —— 也就是 version.json 声明值。
    """
    url = data.get("url", "")
    if not url:
        return {"ok": False, "error": "缺少下载地址"}
    if _update_download_state["downloading"]:
        return {"ok": False, "error": "正在下载中，请稍候"}
    _update_download_state.update({
        "downloading": True, "progress": 0, "total": 0,
        "done": False, "error": None, "file": None,
    })
    sha = (data.get("sha256") or "").strip().lower()
    try:
        size = int(data.get("size") or 0)
    except (TypeError, ValueError):
        size = 0
    asyncio.create_task(_do_download(url, sha, size))
    return {"ok": True}


async def _do_download(url: str, expect_sha256: str = "", expect_size: int = 0):
    """后台下载任务：流式落盘 + 边下边算 sha256 + 下完校验。

    校验不过一律删文件报错。宁可让用户重试，也不能把截断的包换上去 ——
    换上去之后用户双击打不开，且旧版已被改名为 .old，很难自己救回来。
    """
    import tempfile
    # 只取临时文件名，不保持句柄（Windows 下未关闭的句柄会导致后续 open 失败）
    fd, tmp_path = tempfile.mkstemp(suffix=".exe", dir=DATA_DIR)
    os.close(fd)
    digest = hashlib.sha256()
    downloaded = 0
    try:
        async with httpx.AsyncClient(timeout=httpx.Timeout(600, connect=15)) as client:
            async with client.stream("GET", url, follow_redirects=True) as resp:
                if resp.status_code != 200:
                    _update_download_state["error"] = f"下载失败: HTTP {resp.status_code}"
                    _update_download_state["downloading"] = False
                    os.remove(tmp_path)
                    return
                content_length = resp.headers.get("content-length")
                total = int(content_length) if content_length else 0
                _update_download_state["total"] = total
                with open(tmp_path, "wb") as f:
                    async for chunk in resp.aiter_bytes(chunk_size=256 * 1024):
                        f.write(chunk)
                        digest.update(chunk)
                        downloaded += len(chunk)
                        if total > 0:
                            _update_download_state["progress"] = round(downloaded / total * 100, 1)

        # ---- 完整性校验（不通过就抛，由下面的 except 删文件） ----
        if not expect_sha256 and not expect_size:
            logger.warning("version.json 没有为该平台提供 sha256/size，本次更新无法校验完整性")
        _assert_download_ok(digest.hexdigest(), downloaded, expect_sha256, expect_size)

        _update_download_state["done"] = True
        _update_download_state["file"] = tmp_path
        _update_download_state["downloading"] = False
    except Exception as e:
        logger.warning("更新包校验失败，已丢弃：%s", e)
        _update_download_state["error"] = str(e)
        _update_download_state["downloading"] = False
        try:
            os.remove(tmp_path)
        except Exception:
            pass


@app.get("/api/download-progress")
async def download_progress(_=Depends(verify_admin)):
    """返回当前下载进度"""
    return {"ok": True, **{k: v for k, v in _update_download_state.items()}}


@app.post("/api/apply-update")
async def apply_update(_=Depends(verify_admin)):
    """应用更新：替换 exe 并重启"""
    if sys.platform != "win32":
        return {"ok": False,
                "error": "在线热更新仅限 Windows 客户端，请手动更新（git pull / 重新下载）"}
    if not getattr(sys, 'frozen', False):
        return {"ok": False, "error": "开发模式下不支持热更新，请打包后使用"}
    if not _update_download_state["done"] or not _update_download_state["file"]:
        return {"ok": False, "error": "没有可应用的更新"}
    new_file = _update_download_state["file"]
    if not os.path.exists(new_file):
        return {"ok": False, "error": "更新文件不存在"}
    # 最后一道粗筛：不是本平台可执行文件就不碰磁盘上的现有版本
    if not _looks_like_executable(new_file):
        return {"ok": False,
                "error": "更新文件不是本平台的可执行文件（可能下载不完整，"
                         "或下载到了错误页面），已拒绝安装，当前版本未受影响"}
    try:
        _do_swap_and_restart(new_file)
    except Exception as e:
        return {"ok": False, "error": f"更新失败: {e}"}
    return {"ok": True}


def _do_swap_and_restart(new_exe: str):
    """重命名当前 exe → 替换新 exe → 启动新进程 → 退出当前进程。

    文件层面的任何失败（改名失败 / 新包不是有效可执行文件 / 进程拉不起来）
    都回滚到旧 exe。用户最坏的结果是「这次没更新成」，
    而不是「软件打不开了、还得自己去翻 .old」。
    """
    import subprocess
    current_exe = sys.executable
    old_exe = current_exe + ".old"

    # 1. 删除旧残留
    try:
        if os.path.exists(old_exe):
            os.remove(old_exe)
        # 2. 当前 exe 改名为 .old（运行中的 exe 可以改名不能删）
        os.rename(current_exe, old_exe)
        # 3. 新 exe 移到当前 exe 位置
        os.rename(new_exe, current_exe)
    except Exception as e:
        # 改名阶段就失败了：把能恢复的恢复回来，一个文件都别丢
        if not os.path.exists(current_exe) and os.path.exists(old_exe):
            try:
                os.rename(old_exe, current_exe)
            except Exception:
                logger.exception("回滚失败，请手工把 %s 改名为 %s", old_exe, current_exe)
        raise RuntimeError(f"替换文件失败：{e}")

    # 4. 启动新 exe
    creationflags = 0x00000008 if sys.platform == "win32" else 0  # DETACHED_PROCESS
    try:
        subprocess.Popen([current_exe], close_fds=True, creationflags=creationflags)
    except Exception as e:
        # 新包根本拉不起来（不是有效 PE / 缺运行库）：换回旧版再报错
        logger.exception("启动新版本失败，回滚到旧版本")
        try:
            os.remove(current_exe)
            os.rename(old_exe, current_exe)
        except Exception:
            logger.exception("回滚失败，请手工把 %s 改名为 %s", old_exe, current_exe)
        raise RuntimeError(f"新版本无法启动（{e}），已回滚，当前版本可继续使用")

    # 5. 退出（旧版留在 .old，作为最后的人工兜底）
    os._exit(0)


@app.get("/api/providers")
async def list_providers(_=Depends(verify_admin)):
    result = []
    # 只读价目缓存（过期就后台悄悄刷新，不让页面等网络）
    oracle = await get_price_oracle()

    for p in providers:
        item = {
            "name": p["name"],
            "base_url": p["base_url"],
            "api_key_masked": mask_key(p.get("api_key", "")),
            "models": p.get("models", []),
            "disabled_models": p.get("disabled_models", []),
            "free_only": p.get("free_only", True),
            "free_status": summarize_provider_free(p, oracle),
            # 组队来源标记：前端用它显示「来自已解散队伍」并置灰
            "team_origin": p.get("team_origin"),
            "team_status": p.get("team_status"),
            "health": {},
        }
        for m in p.get("models", []):
            k = f"{p['name']}||{m}"
            item["health"][m] = health_status.get(k, {"status": "unknown"})
        result.append(item)
    return result


@app.get("/api/price-source")
async def price_source_info(_=Depends(verify_admin)):
    """公开价目源的元信息（来源、抓取时间、收录模型数），供前端 tooltip 说人话。"""
    oracle = await get_price_oracle()
    return {
        "enabled": _price_oracle_enabled(),
        "source": oracle.get("source"),
        "fetched_at": oracle.get("fetched_at"),
        "models": len(oracle.get("tail") or {}),
    }


@app.post("/api/providers")
async def add_provider(data: ProviderIn, _=Depends(verify_admin)):
    if not re.match(r'^[\u4e00-\u9fa5a-zA-Z0-9_.\-]+$', data.name):
        raise HTTPException(400, "名称只能包含中文、字母、数字、横杠(-)、下划线(_)、点(.)，不能含斜杠/空格等特殊字符")
    # 校验 key 有效性（保存前强制校验）
    vr = await verify_provider_key_impl(data.base_url, data.api_key)
    if not vr["ok"]:
        raise HTTPException(400, f"API Key 校验失败：{vr['detail']}")
    async with providers_lock:
        for p in providers:
            if p["name"] == data.name:
                raise HTTPException(400, "名称已存在")
        if not data.models:
            models, pricing = await fetch_models_with_pricing(
                data.base_url, data.api_key, data.free_only
            )
            data.models = models
            if pricing:
                data.model_pricing = pricing  # 上游自报价目，作为"免费"判定的最权威证据
        providers.append(data.model_dump())
        save_providers(providers)
    return {"ok": True}


@app.post("/api/providers/verify-key")
async def verify_provider_key(data: VerifyKeyIn, _=Depends(verify_admin)):
    """校验上游 base_url + api_key 是否可用"""
    return await verify_provider_key_impl(data.base_url, data.api_key)


@app.post("/api/open-url")
async def open_url(data: OpenUrlIn, _=Depends(verify_admin)):
    """用系统默认浏览器打开外链（pywebview 内 target=_blank 会被拦截，统一走此接口）"""
    url = (data.url or "").strip()
    if not re.match(r'^https?://', url, re.I):
        raise HTTPException(400, "仅允许 http/https 链接")
    try:
        webbrowser.open(url)
        return {"ok": True}
    except Exception as e:
        raise HTTPException(500, f"打开失败: {e}") from e


@app.get("/api/preset-info")
async def preset_info(_=Depends(verify_admin)):
    """返回预设清单（远端热更新优先，内置兜底）"""
    return await load_preset()


@app.get("/api/vision-assist")
async def get_vision_assist(_=Depends(verify_admin)):
    """返回识图辅助开关状态（默认关闭）"""
    cfg = app_config.get("vision_assist", {})
    enabled = cfg.get("enabled", False) if isinstance(cfg, dict) else False
    return {"enabled": enabled}


@app.put("/api/vision-assist")
async def set_vision_assist(data: dict, _=Depends(verify_admin)):
    """开启/关闭识图辅助，并持久化到 config.json"""
    enabled = bool(data.get("enabled", False))
    cfg = app_config.get("vision_assist", {})
    if not isinstance(cfg, dict):
        cfg = {}
    cfg["enabled"] = enabled
    app_config["vision_assist"] = cfg
    save_config()
    return {"enabled": enabled}


@app.post("/api/providers/preset")
async def apply_preset(data: PresetApplyIn, _=Depends(verify_admin)):
    """一键应用预设：三平台逐个校验 key → 创建/覆盖 provider → 合并路由组。
    - 用户填了 Key 的平台：校验 → 覆盖旧配置 → 重新拉模型
    - 用户没填 Key 但已有同名 provider：跳过，保留原配置不变
    - 用户没填 Key 且无同名 provider：标记未配置

    断电保护：保存前先备份，保存后删备份。下次启动若发现备份残留自动恢复。"""
    preset = await load_preset()
    platforms = preset.get("platforms", {})
    keys = data.keys or {}
    overwrite = bool(data.overwrite_existing)
    results = {}
    created_names = []

    # ---- 备份当前数据（防断电） ----
    bak_providers = DATA_FILE.with_suffix(DATA_FILE.suffix + ".preset_bak")
    bak_routers = ROUTERS_FILE.with_suffix(ROUTERS_FILE.suffix + ".preset_bak")
    try:
        if DATA_FILE.exists():
            bak_providers.write_text(DATA_FILE.read_text("utf-8"), "utf-8")
        if ROUTERS_FILE.exists():
            bak_routers.write_text(ROUTERS_FILE.read_text("utf-8"), "utf-8")
    except Exception:
        pass  # 备份失败不阻塞主流程

    try:
        async with providers_lock:
            existing_names = {p["name"] for p in providers}
            for plat_name, plat_cfg in platforms.items():
                key = (keys.get(plat_name) or "").strip()
                coexist = False            # key 不同要并存：旧条目保留，新条目加后缀
                # needs_key=False 的平台（Ollama / LM Studio 这类本机推理）压根没有
                # Key 输入框，keys 里自然不存在它。若沿用下面的老逻辑，会一律落到
                # 「未填写 Key」被跳过 —— 卡片摆在那却永远配不上，正是无效控件。
                # 所以这类平台跳过 Key 校验，直接用空 Key 走探测（空 Key 对
                # 本机推理服务是正确的，转发时 header 就是 Bearer 空值）。
                no_key_needed = plat_cfg.get("needs_key") is False

                # 已配置的平台怎么处理，分三种情况：
                #   没填 Key / 填了相同的 Key 且没勾「重新拉取」→ 跳过
                #   填了**不同的 Key** → 新增一条并存，旧的原封不动
                #   勾了「重新拉取」且 Key 相同 → 刷新（重置 free_only 等，属预期）
                # 最后一种是唯一会动旧条目的情形。
                existing = next((p for p in providers if p["name"] == plat_name), None)
                if existing is not None:
                    team_src = existing.get("team_origin")
                    same_key = (existing.get("api_key") or "") == key
                    if key and not same_key:
                        # key 不同 → 并存。团队 14 人各自注册免费账号，
                        # 覆盖等于把别人的额度顶掉，正好违背组队初衷。
                        same_key = False
                    elif not overwrite:
                        results[plat_name] = {
                            "ok": True,
                            "detail": (f"已配置，本次跳过（来自队伍 {team_src}）"
                                       if team_src else "已配置，本次跳过"),
                        }
                        continue
                    # 走到这里要么是「key 不同要并存」，要么是「勾了重新拉取」。
                    # 并存时**不删旧条目**，改用带后缀的新名字。
                    coexist = key and not same_key

                if not key and not no_key_needed:
                    if plat_name in existing_names:
                        existing = next((p for p in providers if p["name"] == plat_name), None)
                        if existing and existing.get("api_key"):
                            key = existing["api_key"]
                        else:
                            results[plat_name] = {"ok": True, "detail": "保留已有配置（无可用 Key）"}
                            continue
                    else:
                        results[plat_name] = {"ok": False, "detail": "未填写 Key"}
                        continue
                vr = await verify_provider_key_impl(plat_cfg["base_url"], key)
                if not vr["ok"]:
                    results[plat_name] = {"ok": False, "detail": vr["detail"]}
                    continue
                final_name = plat_name
                if plat_name in existing_names:
                    if coexist:
                        # 并存：旧条目原封不动，新条目换个名字。
                        # 用户口径是「同一个上游只要 key 不一样就不要覆盖，
                        # 当作不同的上游供应商看待」。
                        k = 2
                        while f"{plat_name}-{k}" in existing_names:
                            k += 1
                        final_name = f"{plat_name}-{k}"
                    else:
                        providers[:] = [p for p in providers if p["name"] != plat_name]
                        existing_names.discard(plat_name)
                fetched, pricing = await fetch_models_with_pricing(
                    plat_cfg["base_url"], key, plat_cfg.get("free_only", True)
                )
                visible = plat_cfg.get("models_visible", [])
                if visible:
                    models = [m for m in fetched if m in set(visible)]
                    for m in visible:
                        if m not in models:
                            models.append(m)
                    disabled = []
                else:
                    models = fetched
                    disabled = []
                providers.append({
                    "name": final_name,
                    "base_url": plat_cfg["base_url"],
                    "api_key": key,
                    "models": models,
                    "disabled_models": disabled,
                    "free_only": plat_cfg.get("free_only", True),
                    **({"model_pricing": pricing} if pricing else {}),
                })
                existing_names.add(final_name)
                created_names.append(final_name)
                results[plat_name] = {
                    "ok": True,
                    "detail": (f"已新增为「{final_name}」（Key 不同，旧的保留）"
                               if coexist else f"已配置 {len(models)} 个模型")}
            save_providers(providers)
            preset_routers = preset.get("routers", {})
            for gname, members in preset_routers.items():
                ROUTERS[gname] = list(members)
            save_routers()
    except Exception:
        raise
    finally:
        # ---- 保存成功，删除备份 ----
        try:
            if bak_providers.exists():
                bak_providers.unlink()
            if bak_routers.exists():
                bak_routers.unlink()
        except Exception:
            pass

    return {"ok": True, "results": results, "created": created_names}


@app.post("/api/providers/{name}/fetch-models")
async def refresh_models(name: str, _=Depends(verify_admin)):
    async with providers_lock:
        for p in providers:
            if p["name"] == name:
                models, pricing = await fetch_models_with_pricing(
                    p["base_url"], p["api_key"], p.get("free_only", True)
                )
                if models:
                    p["models"] = models
                    if pricing:
                        p["model_pricing"] = pricing  # 存下上游自报价目
                    save_providers(providers)
                return {"ok": True, "models": models}
    raise HTTPException(404, "未找到")


@app.get("/api/providers/{name}/available-models")
async def get_available_models(name: str, _=Depends(verify_admin)):
    """上游全量模型列表 + 每个模型的免费判定（模型管理弹窗用）。"""
    target = None
    for p in providers:
        if p["name"] == name:
            target = p
            break
    if target is None:
        raise HTTPException(404, "未找到")

    models, fresh_pricing = await fetch_models_with_pricing(
        target["base_url"], target["api_key"], free_only=False
    )
    if not models:
        return {"ok": True, "models": [], "free": {}}

    # 顺手把上游自报价目存下来（这是 E1 级最权威证据，下次不用重拉也能判）
    stored = dict(target.get("model_pricing") or {})
    if fresh_pricing:
        stored.update(fresh_pricing)
        if stored != (target.get("model_pricing") or {}):
            async with providers_lock:
                target["model_pricing"] = stored
                save_providers(providers)

    oracle = await get_price_oracle()
    free_map = {
        m: classify_model_free(
            m,
            upstream_pricing=stored.get(m),
            base_url=target["base_url"],
            oracle=oracle,
        )
        for m in models
    }
    return {"ok": True, "models": models, "free": free_map}


@app.put("/api/providers/{name}")
async def update_provider(name: str, data: ProviderUpdate, _=Depends(verify_admin)):
    async with providers_lock:
        for i, p in enumerate(providers):
            if p["name"] == name:
                providers[i].update(data.model_dump(exclude_unset=True))
                save_providers(providers)
                return {"ok": True}
    raise HTTPException(404, "未找到")


@app.delete("/api/providers/{name}")
async def delete_provider(name: str, _=Depends(verify_admin)):
    global providers
    async with providers_lock:
        providers = [p for p in providers if p["name"] != name]
        save_providers(providers)
    return {"ok": True}


@app.post("/api/providers/{name}/toggle-model")
async def toggle_model(name: str, data: ToggleModelIn, _=Depends(verify_admin)):
    async with providers_lock:
        for p in providers:
            if p["name"] == name:
                disabled = p.get("disabled_models", [])
                if data.enabled:
                    if data.model in disabled:
                        disabled.remove(data.model)
                else:
                    if data.model not in disabled:
                        disabled.append(data.model)
                p["disabled_models"] = disabled
                save_providers(providers)
                return {"ok": True, "disabled_models": disabled}
    raise HTTPException(404, "未找到")


@app.post("/api/check/{name}/{model}")
async def manual_check(name: str, model: str, _=Depends(verify_admin)):

    for p in providers:
        if p["name"] == name:
            result = await check_model(p["base_url"], p["api_key"], model)
            k = f"{name}||{model}"
            result["checked_at"] = time.time()
            health_status[k] = result
            update_model_quality(k, result)
            await append_history({k: result})
            return result
    raise HTTPException(404, "未找到")


@app.post("/api/check/all")
async def check_all(_=Depends(verify_admin)):
    tasks = []
    for p in list(providers):
        for m in get_enabled_models(p):
            tasks.append((p["name"], m, p["base_url"], p["api_key"]))
    results = await run_health_checks(tasks)
    health_status.update(results)
    await append_history(results)
    mark_full_check()
    return results


# ============================================================
# 预设模板（三层加载：远端热更新 → 内置兜底）
# ============================================================
PRESET_REMOTE_URL = "https://raw.githubusercontent.com/chenshifanjian/model-reservoir/revive/presets.json"
PRESET_DOC_URL = "https://pv284bk9no6.feishu.cn/wiki/HCOuwXuZGibDUGkWLlpcQuiLnDf"
PRESET_CACHE_TTL = 300

# 内置兜底预设（断网保底；平台变更时改远端 presets.json 热更新即可，无需重新打包）
# 内置预设的两个补充字段（免费档接入）：
#   needs_key   False = 免 key，本机推理服务，开箱即用；True = 要免费注册拿 key（不绑卡）
#   free_quota  人话描述免费额度。**不硬编码模型名** —— Cerebras 曾从十几个模型
#               掉到 2 个，目录波动是免费档常态，写死模型名会立刻失效
BUILTIN_PRESET = {
    "version": "2026-10-19",
    "updated_at": "2026-10-19",
    "doc_url": PRESET_DOC_URL,
    "platforms": {
        # ---- 免 key：本机推理，开箱即用 ----
        "Ollama": {
            "base_url": "http://127.0.0.1:11434/v1",
            "free_only": False,
            "needs_key": False,
            "free_quota": "无限（吃本机算力）",
            "key_page_url": "https://ollama.com/download",
            "auth_hint": "无需 Key，先装 Ollama",
            "models": [],
        },
        "LM Studio": {
            "base_url": "http://127.0.0.1:1234/v1",
            "free_only": False,
            "needs_key": False,
            "free_quota": "无限（吃本机算力）",
            "key_page_url": "https://lmstudio.ai/download",
            "auth_hint": "无需 Key，先装 LM Studio 并开启本地服务",
            "models": [],
        },
        # ---- 免费注册拿 key（都不绑卡）----
        "OpenRouter": {
            "base_url": "https://openrouter.ai/api/v1",
            "free_only": False,
            "needs_key": True,
            "free_quota": "免费模型带 :free 后缀，约 20 请求/分、50 请求/天",
            "key_page_url": "https://openrouter.ai/keys",
            "auth_hint": "免费注册，不绑卡",
            "models": [],
        },
        "Groq": {
            "base_url": "https://api.groq.com/openai/v1",
            "free_only": False,
            "needs_key": True,
            "free_quota": "约 30 请求/分，推理速度极快",
            "key_page_url": "https://console.groq.com/keys",
            "auth_hint": "免费注册，不绑卡",
            "models": [],
        },
        "Cerebras": {
            "base_url": "https://api.cerebras.ai/v1",
            "free_only": False,
            "needs_key": True,
            "free_quota": "约 100 万 token/天（目录波动大，别硬编码模型名）",
            "key_page_url": "https://cloud.cerebras.ai/",
            "auth_hint": "免费注册，不绑卡",
            "models": [],
        },
        "Google Gemini": {
            "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
            "free_only": False,
            "needs_key": True,
            "free_quota": "约 1500 请求/天（AI Studio 免费档）",
            "key_page_url": "https://aistudio.google.com/apikey",
            "auth_hint": "免费注册，不绑卡",
            "models": [],
        },
        "GitHub Models": {
            "base_url": "https://models.inference.ai.azure.com",
            "free_only": False,
            "needs_key": True,
            "free_quota": "约 15 请求/分、150 请求/天",
            "key_page_url": "https://github.com/marketplace/models",
            "auth_hint": "用 GitHub 账号换 Token",
            "models": [],
        },
        # ---- 原有三个 ----
        "NVIDIA": {
            "base_url": "https://integrate.api.nvidia.com/v1",
            "free_only": True,
            "needs_key": True,
            "free_quota": "免费档（2026-07-23 实测模型列表全空，疑似活动已结束，配前先测）",
            "key_page_url": "https://build.nvidia.com/",
            "auth_hint": "需绑定手机号",
            "models_visible": [
                "deepseek-ai/deepseek-v4-flash",
                "deepseek-ai/deepseek-v4-pro",
                "minimaxai/minimax-m3",
                "mistralai/mistral-large-3-675b-instruct-2512",
                "mistralai/mistral-small-4-119b-2603",
                "nvidia/nemotron-3-super-120b-a12b",
                "nvidia/nemotron-3-ultra-550b-a55b",
                "qwen/qwen3.5-122b-a10b",
                "z-ai/glm-5.2",
                "meta/llama-3.2-11b-vision-instruct",
                "nvidia/nemotron-nano-12b-v2-vl",
                "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
            ],
        },
        "SenseNova": {
            "base_url": "https://token.sensenova.cn/v1",
            "free_only": False,
            "needs_key": True,
            "free_quota": "需手机号注册，有免费额度",
            "key_page_url": "https://platform.sensenova.cn/console/keys",
            "auth_hint": "手机号注册登录即可",
            "models_visible": [
                "deepseek-v4-flash",
                "glm-5.2",
                "sensenova-6.7-flash-lite",
            ],
        },
        "魔搭": {
            "base_url": "https://api-inference.modelscope.cn/v1",
            "free_only": False,
            "needs_key": True,
            "free_quota": "部分模型免费，需阿里云账号（支付宝实名）",
            "key_page_url": "https://modelscope.cn/my/myaccesstoken",
            "auth_hint": "需绑定阿里云账号（支付宝实名）",
            "models_visible": [
                "Qwen/Qwen3.5-122B-A10B",
                "Qwen/Qwen3.5-397B-A17B",
                "deepseek-ai/DeepSeek-V4-Flash",
                "deepseek-ai/DeepSeek-V4-Pro",
                "OpenGVLab/InternVL3_5-241B-A28B",
                "Qwen/Qwen3-VL-8B-Thinking",
                "Qwen/Qwen3-VL-8B-Instruct",
                "PaddlePaddle/ERNIE-4.5-VL-28B-A3B-PT",
            ],
        },
    },
    "routers": {
        "256k": [
            "mistralai/mistral-large-3-675b-instruct-2512",
            "mistralai/mistral-small-4-119b-2603",
            "nvidia/nemotron-3-super-120b-a12b",
            "nvidia/nemotron-3-ultra-550b-a55b",
            "qwen/qwen3.5-122b-a10b",
            "sensenova-6.7-flash-lite",
            "Qwen/Qwen3.5-122B-A10B",
            "Qwen/Qwen3.5-397B-A17B",
        ],
        "1m": [
            "deepseek-ai/deepseek-v4-pro",
            "minimaxai/minimax-m3",
            "z-ai/glm-5.2",
            "glm-5.2",
            "deepseek-ai/DeepSeek-V4-Pro",
            "deepseek-ai/deepseek-v4-flash",
            "deepseek-v4-flash",
            "deepseek-ai/DeepSeek-V4-Flash",
        ],
        "识图": [
            "sensenova-6.7-flash-lite",
            "mistralai/mistral-large-3-675b-instruct-2512",
            "mistralai/mistral-small-4-119b-2603",
            "meta/llama-3.2-11b-vision-instruct",
            "nvidia/nemotron-nano-12b-v2-vl",
            "nvidia/llama-3.1-nemotron-nano-vl-8b-v1",
            "OpenGVLab/InternVL3_5-241B-A28B",
            "Qwen/Qwen3-VL-8B-Thinking",
            "Qwen/Qwen3-VL-8B-Instruct",
            "PaddlePaddle/ERNIE-4.5-VL-28B-A3B-PT",
        ],
    },
}

_preset_cache = {"data": None, "ts": 0.0}


async def load_preset(force_remote: bool = False) -> dict:
    """三层加载：远端热更新(优先) → 内置兜底。缓存 PRESET_CACHE_TTL 秒。"""
    now = time.time()
    if (not force_remote and _preset_cache["data"]
            and now - _preset_cache["ts"] < PRESET_CACHE_TTL):
        return _preset_cache["data"]
    if http_client:
        try:
            resp = await http_client.get(PRESET_REMOTE_URL, timeout=10)
            if resp.status_code == 200:
                data = resp.json()
                if isinstance(data, dict) and data.get("platforms"):
                    _preset_cache["data"] = data
                    _preset_cache["ts"] = now
                    return data
        except Exception:
            logger.warning("load_preset remote fetch failed, fallback to builtin")
    if not _preset_cache["data"]:
        _preset_cache["data"] = BUILTIN_PRESET
        _preset_cache["ts"] = now
    return _preset_cache["data"]


def has_image(body: dict) -> bool:
    """检测 messages 是否含 image_url（仅看最后一轮 user 消息，历史图片不算）"""
    msgs = body.get("messages", [])
    for i in range(len(msgs) - 1, -1, -1):
        msg = msgs[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "image_url":
                        return True
            return False  # 最后一轮 user 消息没有图，不再往前看
    return False


CN_HINT = "（请用简体中文回答）"

def _inject_cn_hint(body: dict):
    """将中文回复指令注入最后一条 user 消息，确保识图模型用中文回答。
    部分小模型会忽略 system prompt，直接塞进用户消息最可靠。"""
    msgs = body.get("messages")
    if not isinstance(msgs, list):
        return
    for i in range(len(msgs) - 1, -1, -1):
        msg = msgs[i]
        if isinstance(msg, dict) and msg.get("role") == "user":
            content = msg.get("content")
            if isinstance(content, str) and CN_HINT not in content:
                msg["content"] = content + "\n" + CN_HINT
            elif isinstance(content, list):
                for part in content:
                    if isinstance(part, dict) and part.get("type") == "text":
                        t = part.get("text", "")
                        if CN_HINT not in t:
                            part["text"] = t + "\n" + CN_HINT
                        break
            break


# ============================================================
# 代理（客户端鉴权）
# ============================================================
async def _stream_with_failover(candidates, body, is_router, prelude: str = ""):
    """流式转发，中断时自动切换下一个候选模型继续输出。prelude 为先输出给用户的提示文本。"""

    async def gen():
        accumulated = ""
        prefix_done = False
        last_err = ""
        max_attempts = 2 if is_router else 1

        if prelude:
            yield "data: " + json.dumps({"choices": [{"delta": {"content": prelude}, "index": 0}]}, ensure_ascii=False) + "\n\n"

        for _attempt in range(max_attempts):
            for provider, model in candidates:
                k = f"{provider['name']}||{model}"
                req_body = copy.deepcopy(body)
                req_body["model"] = MODEL_ALIASES.get(model, model)

                if accumulated:
                    msgs = list(req_body.get("messages", []))
                    msgs.append({"role": "assistant", "content": accumulated})
                    msgs.append({"role": "user", "content": "请继续上面的回复，从中断处接着写。"})
                    req_body["messages"] = msgs

                url = provider["base_url"].rstrip("/") + "/chat/completions"
                headers = {
                    **auth_headers(provider["api_key"]),
                    "Content-Type": "application/json",
                }

                req = http_client.build_request("POST", url, json=req_body, headers=headers)
                try:
                    resp = await http_client.send(req, stream=True)
                except httpx.RequestError as e:
                    logger.warning("stream connect error to %s: %s", provider["name"], e)
                    record_fail(k)
                    call_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "ts": time.time(),  # 前端相对时间显示用
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": "连接失败",
                    })
                    continue

                if resp.status_code != 200:
                    try:
                        await resp.aread()
                    except Exception:
                        pass
                    await resp.aclose()
                    logger.warning("upstream stream error %d from %s", resp.status_code, provider["name"])
                    if _provider_fault(resp.status_code):
                        record_fail(k)
                    last_err = f"upstream {resp.status_code}"
                    call_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "ts": time.time(),  # 前端相对时间显示用
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": f"HTTP {resp.status_code}",
                    })
                    continue

                usage_obj = None
                stream_ok = True

                try:
                    async for line in resp.aiter_lines():
                        if not line:
                            continue
                        if not line.startswith("data: "):
                            yield line + "\n"
                            continue
                        data_str = line[6:]
                        if data_str.strip() == "[DONE]":
                            yield "data: [DONE]\n\n"
                            break
                        try:
                            obj = json.loads(data_str)
                            if obj.get("usage"):
                                usage_obj = obj["usage"]
                            if "model" in obj and isinstance(obj["model"], str):
                                obj["model"] = local_model_id(provider["name"], model)
                            choices = obj.get("choices") or []
                            if choices:
                                delta = choices[0].get("delta") or {}
                                c = delta.get("content")
                                if isinstance(c, str):
                                    accumulated += c
                                rc = delta.get("reasoning_content")
                                if isinstance(rc, str):
                                    accumulated += rc
                            if not prefix_done:
                                choices2 = obj.get("choices") or []
                                if choices2:
                                    delta2 = choices2[0].get("delta") or {}
                                    c2 = delta2.get("content")
                                    if isinstance(c2, str) and c2:
                                        delta2["content"] = f"🤖 {local_model_id(provider['name'], model)}\n\n{c2}"
                                        prefix_done = True
                            out = json.dumps(obj, ensure_ascii=False)
                            out = restore_hermes_text(out)
                            yield "data: " + out + "\n\n"
                        except json.JSONDecodeError:
                            yield line + "\n"
                    record_success(k)
                    try:
                        pt = (usage_obj or {}).get("prompt_tokens", 0) or 0
                        ct = (usage_obj or {}).get("completion_tokens", 0) or 0
                        await append_usage({
                            "ts": time.time(), "model": model,
                            "provider": provider["name"],
                            "pt": pt, "ct": ct, "tt": pt + ct,
                        })
                        call_log.append({
                            "time": time.strftime("%H:%M:%S"),
                            "ts": time.time(),  # 前端相对时间显示用
                            "provider": provider["name"],
                            "model": model,
                            "status": "ok",
                            "tokens": pt + ct,
                        })
                    except Exception:
                        logger.exception("append_usage(stream) failed")
                    return
                except Exception:
                    stream_ok = False
                    logger.exception("stream interrupted from %s, switching", provider["name"])
                    record_fail(k)
                    call_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "ts": time.time(),  # 前端相对时间显示用
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": "流中断",
                    })
                    try:
                        await resp.aclose()
                    except Exception:
                        pass
                    continue
                finally:
                    if stream_ok:
                        try:
                            await resp.aclose()
                        except Exception:
                            pass

        tail = "\n\n⚠️ 所有模型均失败（%s），回复中断。" % (last_err or "无可用候选")
        yield "data: " + json.dumps({"choices": [{"delta": {"content": tail}, "index": 0}]}, ensure_ascii=False) + "\n\n"
        yield "data: [DONE]\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream")


@app.api_route("/v1/chat/completions", methods=["POST"], dependencies=[Depends(verify_client)])
async def proxy_chat(request: Request, force: bool = False):
    body = await request.json()
    body = compress_hermes(body)
    body = ensure_lang_reply(body)
    requested_model = body.get("model")

    # 识图辅助：含图片且目标非识图组/识图模型 → 直接转交识图路由组
    vision_cfg = app_config.get("vision_assist", {})
    vision_enabled = vision_cfg.get("enabled", False) if isinstance(vision_cfg, dict) else False
    is_vision_request = (requested_model == "识图") or bool(requested_model and is_vision_model(requested_model))
    vision_prelude = ""
    if vision_enabled and has_image(body) and not is_vision_request:
        if "识图" in ROUTERS:
            requested_model = "识图"
            vision_prelude = "🖼️ 已切换到视觉模型回复…\n\n"
            # 识图模型 max_tokens 上限较低（部分仅 32768），避免客户端传的百万级值导致 upstream 400
            for key in ("max_tokens", "max_completion_tokens"):
                if body.get(key, 0) > 16384:
                    body[key] = 16384
            # 部分识图模型忽略 system prompt，把中文指令直接注入用户消息末尾
            _inject_cn_hint(body)
        else:
            raise HTTPException(503, "识图辅助已开启，但未配置识图路由组，无法处理图片。")

    candidates = pick_available_models(requested_model, force=force)
    if not candidates:
        raise HTTPException(503, f"无可用的模型: {requested_model or '任意'}")

    is_router = requested_model in ROUTERS
    stream = body.get("stream", False)
    last_err = None
    last_client_error = None   # (status, body) —— 全部失败时原样回传给调用方

    if stream:
        return await _stream_with_failover(candidates, body, is_router, prelude=vision_prelude)

    for _attempt in (2,) if is_router else (1,):
        for provider, model in candidates:
            k = f"{provider['name']}||{model}"
            req_body = copy.deepcopy(body)
            req_body["model"] = MODEL_ALIASES.get(model, model)
            url = provider["base_url"].rstrip("/") + "/chat/completions"
            headers = {
                **auth_headers(provider["api_key"]),
                "Content-Type": "application/json",
            }

            try:
                resp = await http_client.post(url, json=req_body, headers=headers, timeout=120)
                if resp.status_code >= 400:
                    logger.warning("upstream %d from %s: %s", resp.status_code, provider["name"], resp.text[:200])
                    if _provider_fault(resp.status_code):
                        record_fail(k)
                    last_err = f"upstream {resp.status_code}"
                    if 400 <= resp.status_code < 500:
                        # 4xx 是请求本身的问题，把上游原话记住：全部候选都失败时原样回传，
                        # 否则用户只会看到 502「upstream 400」，永远不知道哪个参数错了。
                        last_client_error = (resp.status_code, resp.text)
                    call_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "ts": time.time(),  # 前端相对时间显示用
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": f"HTTP {resp.status_code}",
                    })
                    continue
                try:
                    parsed = json.loads(resp.text)
                    parsed = merge_reasoning(parsed)
                    parsed_str = json.dumps(parsed, ensure_ascii=False)
                    parsed_str = restore_hermes_text(parsed_str)
                    parsed = json.loads(parsed_str)
                    record_success(k)
                    parsed["model"] = local_model_id(provider["name"], model)
                    try:
                        u = parsed.get("usage") or {}
                        pt = u.get("prompt_tokens", 0) or 0
                        ct = u.get("completion_tokens", 0) or 0
                        await append_usage({
                            "ts": time.time(), "model": model,
                            "provider": provider["name"],
                            "pt": pt, "ct": ct, "tt": pt + ct,
                        })
                        # 调用日志记录
                        call_log.append({
                            "time": time.strftime("%H:%M:%S"),
                            "ts": time.time(),  # 前端相对时间显示用
                            "provider": provider["name"],
                            "model": model,
                            "status": "ok",
                            "tokens": pt + ct,
                        })
                    except Exception:
                        logger.exception("append_usage(non-stream) failed")
                    try:
                        msg = parsed["choices"][0]["message"]
                        c = msg.get("content")
                        prefix_parts = []
                        if vision_prelude:
                            prefix_parts.append(vision_prelude.rstrip())
                        prefix_parts.append(f"🤖 {local_model_id(provider['name'], model)}")
                        prefix = "\n\n".join(prefix_parts)
                        if isinstance(c, str) and c:
                            msg["content"] = f"{prefix}\n\n{c}"
                        elif isinstance(c, str):
                            msg["content"] = prefix
                    except (KeyError, IndexError, TypeError):
                        pass
                    return JSONResponse(content=parsed, status_code=resp.status_code)
                except json.JSONDecodeError:
                    logger.warning("upstream non-json from %s: %s", provider["name"], resp.text[:200])
                    record_fail(k)
                    last_err = f"upstream non-json ({resp.status_code})"
                    call_log.append({
                        "time": time.strftime("%H:%M:%S"),
                        "ts": time.time(),  # 前端相对时间显示用
                        "provider": provider["name"],
                        "model": model,
                        "status": "fail",
                        "tokens": 0,
                        "error": "响应格式错误",
                    })
                    continue
            except httpx.RequestError as e:
                logger.warning("forward error to %s: %s", provider["name"], e)
                record_fail(k)
                last_err = str(e)
                call_log.append({
                    "time": time.strftime("%H:%M:%S"),
                    "ts": time.time(),  # 前端相对时间显示用
                    "provider": provider["name"],
                    "model": model,
                    "status": "fail",
                    "tokens": 0,
                    "error": "连接失败",
                })
                continue
            except Exception as e:
                logger.exception("unexpected forward error to %s", provider["name"])
                record_fail(k)
                last_err = str(e)
                call_log.append({
                    "time": time.strftime("%H:%M:%S"),
                    "ts": time.time(),  # 前端相对时间显示用
                    "provider": provider["name"],
                    "model": model,
                    "status": "fail",
                    "tokens": 0,
                    "error": "未知错误",
                })
                continue

    if last_client_error is not None:
        status, raw = last_client_error
        try:
            payload = json.loads(raw)
            if not isinstance(payload, dict):
                raise ValueError
        except Exception:
            payload = {"error": {"message": raw[:2000] or f"upstream {status}",
                                 "type": "upstream_error", "code": status}}
        logger.info("all candidates failed with client error %d, passing through", status)
        return JSONResponse(content=payload, status_code=status)

    raise HTTPException(502, f"所有候选模型均失败: {last_err}")


_models_cache = {"ts": 0, "data": None}
MODELS_CACHE_TTL = 30


# ============================================================
# 调用日志
# ============================================================
@app.get("/api/call-log")
async def get_call_log(_=Depends(verify_admin)):
    return list(call_log)


# ============================================================
# 配置 API（API Key、端口等）
# ============================================================
@app.put("/api/config")
async def update_config(request: Request, _=Depends(verify_admin)):
    """更新 local_api_key 等配置项"""
    global LOCAL_API_KEY
    body = await request.json()
    if "local_api_key" in body:
        new_key = body["local_api_key"].strip()
        if new_key:
            app_config["local_api_key"] = new_key
            LOCAL_API_KEY = new_key
            save_config()
            return {"ok": True, "key": new_key}
        else:
            raise HTTPException(400, "API Key 不能为空")
    return {"ok": False, "error": "无可更新的字段"}


@app.put("/api/port")
async def update_port(request: Request, _=Depends(verify_admin)):
    """更新端口并触发重启"""
    body = await request.json()
    new_port = int(body.get("port", 8000))
    if new_port < 1024 or new_port > 65535:
        raise HTTPException(400, "端口号范围: 1024-65535")
    app_config["port"] = new_port
    save_config()
    # 延迟重启，让 API 先返回
    def restart():
        time.sleep(0.5)
        if getattr(sys, 'frozen', False):
            subprocess.Popen([sys.executable], close_fds=True,
                           creationflags=0x00000008 if sys.platform == "win32" else 0)
        else:
            subprocess.Popen([sys.executable, str(Path(__file__).resolve())], close_fds=True)
        os._exit(0)
    threading.Thread(target=restart, daemon=True).start()
    return {"ok": True, "port": new_port, "message": "端口已保存，程序即将重启"}


# ============================================================
# 开机自启动
# ============================================================
STARTUP_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "ModelGateway"
# Linux: XDG autostart entry
AUTOSTART_DESKTOP = Path.home() / ".config" / "autostart" / "model-gateway.desktop"


def _autostart_desktop_content() -> str:
    if getattr(sys, "frozen", False):
        # 打包后 sys.executable 就是二进制本体；而 __file__ 指向 onefile 的临时解包目录
        # （进程退出即删）。若照搬源码写法，Exec 里会多出一个失效的 .py 参数，故只写二进制，
        # 并带上 AUTO_KILL 以便登录时接管残留实例。
        exec_line = f'env GATEWAY_AUTO_KILL=1 "{sys.executable}"'
    else:
        exec_line = f"{sys.executable} {Path(__file__).resolve()}"
    return (
        "[Desktop Entry]\n"
        "Type=Application\n"
        "Name=模型蓄水池 (Model Reservoir)\n"
        f"Exec={exec_line}\n"
        "X-GNOME-Autostart-enabled=true\n"
        "NoDisplay=true\n"
    )


# macOS: LaunchAgent plist
LAUNCH_PLIST = (Path.home() / "Library" / "LaunchAgents"
                / "com.modelgateway.client.plist")


def _autostart_plist_content() -> str:
    if getattr(sys, 'frozen', False):
        prog = f"    <string>{sys.executable}</string>\n"
        args = ""
    else:
        prog = f"    <string>{sys.executable}</string>\n"
        args = f"    <string>{Path(__file__).resolve()}</string>\n"
    return (
        "<?xml version=\"1.0\" encoding=\"UTF-8\"?>\n"
        "<!DOCTYPE plist PUBLIC \"-//Apple//DTD PLIST 1.0//EN\" "
        "\"http://www.apple.com/DTDs/PropertyList-1.0.dtd\">\n"
        "<plist version=\"1.0\">\n"
        "<dict>\n"
        "  <key>Label</key>\n"
        "  <string>com.modelgateway.client</string>\n"
        "  <key>ProgramArguments</key>\n"
        "  <array>\n"
        f"{prog}{args}"
        "  </array>\n"
        "  <key>RunAtLoad</key>\n"
        "  <true/>\n"
        "</dict>\n"
        "</plist>\n"
    )


@app.get("/api/autostart")
async def get_autostart(_=Depends(verify_admin)):
    if sys.platform == "darwin":
        return {"enabled": LAUNCH_PLIST.exists()}
    if winreg is None:
        return {"enabled": AUTOSTART_DESKTOP.exists()}
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_READ)
        winreg.QueryValueEx(key, VALUE_NAME)
        winreg.CloseKey(key)
        return {"enabled": True}
    except FileNotFoundError:
        return {"enabled": False}
    except Exception:
        return {"enabled": False}


@app.post("/api/autostart")
async def set_autostart(request: Request, _=Depends(verify_admin)):
    body = await request.json()
    enabled = bool(body.get("enabled", False))
    if sys.platform == "darwin":
        try:
            if enabled:
                LAUNCH_PLIST.parent.mkdir(parents=True, exist_ok=True)
                LAUNCH_PLIST.write_text(_autostart_plist_content(),
                                        encoding="utf-8")
                subprocess.run(["launchctl", "unload", str(LAUNCH_PLIST)],
                               capture_output=True, timeout=15)
                subprocess.run(["launchctl", "load", "-w", str(LAUNCH_PLIST)],
                               capture_output=True, timeout=15)
            else:
                subprocess.run(["launchctl", "unload", "-w", str(LAUNCH_PLIST)],
                               capture_output=True, timeout=15)
                if LAUNCH_PLIST.exists():
                    LAUNCH_PLIST.unlink()
            return {"ok": True, "enabled": enabled}
        except Exception as e:
            raise HTTPException(500, f"操作失败: {e}") from e
    if winreg is None:
        try:
            if enabled:
                AUTOSTART_DESKTOP.parent.mkdir(parents=True, exist_ok=True)
                AUTOSTART_DESKTOP.write_text(_autostart_desktop_content(),
                                             encoding="utf-8")
            elif AUTOSTART_DESKTOP.exists():
                AUTOSTART_DESKTOP.unlink()
            return {"ok": True, "enabled": enabled}
        except Exception as e:
            raise HTTPException(500, f"操作失败: {e}") from e
    try:
        key = winreg.OpenKey(winreg.HKEY_CURRENT_USER, STARTUP_KEY, 0,
                            winreg.KEY_SET_VALUE)
        if enabled:
            exe_path = sys.executable
            winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ,
                            f'"{exe_path}"')
        else:
            try:
                winreg.DeleteValue(key, VALUE_NAME)
            except FileNotFoundError:
                pass
        winreg.CloseKey(key)
        return {"ok": True, "enabled": enabled}
    except Exception as e:
        raise HTTPException(500, f"操作失败: {e}") from e


@app.api_route("/v1/models", methods=["GET"], dependencies=[Depends(verify_client)])
async def proxy_models():
    now = time.time()
    if _models_cache["data"] and now - _models_cache["ts"] < MODELS_CACHE_TTL:
        return _models_cache["data"]
    models_list = []

    # 自定义路由组作为可输出的模型
    for router_name in ROUTERS:
        models_list.append({
            "id": router_name,
            "object": "model",
            "owned_by": "Router",
            "available": True,
            "modality": "router",
            "reasoning": False,
        })

    for p in providers:
        disabled = set(p.get("disabled_models", []))
        for m in p.get("models", []):
            if m in disabled:
                continue
            k = f"{p['name']}||{m}"
            st = health_status.get(k, {}).get("status")
            # 三态：unknown/None -> True（乐观），ok -> True，fail/error -> False
            available = st in (None, "unknown", "ok")
            ctx_len = get_context_length(m)
            caps = infer_capabilities(m)
            models_list.append({
                "id": local_model_id(p["name"], m),
                "object": "model",
                "owned_by": p["name"],
                "available": available,
                "context_length": ctx_len,
                "max_position_embeddings": ctx_len,
                "max_model_len": ctx_len,
                "modality": caps["modality"],
                "reasoning": caps["reasoning"],
            })
    result = {"object": "list", "data": models_list}
    _models_cache["data"] = result
    _models_cache["ts"] = now
    return result


# ============================================================
# AUTO_KILL：只杀本网关进程（源码 app.py / 打包客户端），绝不误杀同端口的陌生程序
# ============================================================
_GATEWAY_MARKERS = ("app.py", "model-gateway", "gateway", "reservoir")


def _looks_like_gateway(info: str) -> bool:
    """进程信息（cmdline/镜像名）里是否带本网关的标记。"""
    if not info:
        return False
    low = info.lower()
    return ("网关" in info or "蓄水池" in info) or any(m in low for m in _GATEWAY_MARKERS)


def _pid_info(pid: int) -> str:
    """取进程信息：Linux 读 /proc、macOS 用 ps、Windows 取 tasklist 镜像名。"""
    try:
        if sys.platform.startswith("linux"):
            with open(f"/proc/{pid}/cmdline", "rb") as fh:
                return fh.read().replace(b"\x00", b" ").decode("utf-8", "replace")
        if sys.platform == "darwin":
            return subprocess.run(["ps", "-o", "args=", "-p", str(pid)],
                                  capture_output=True, text=True, timeout=10).stdout
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/FO", "CSV", "/NH"],
                             capture_output=True, text=True, timeout=10).stdout
        return out.split('","')[0].strip('"') if '","' in out else out
    except Exception:
        return ""


def _pid_is_gateway(pid: int) -> bool:
    if pid <= 0 or pid == os.getpid():
        return False
    return _looks_like_gateway(_pid_info(pid))


def kill_old_instance(port: int) -> bool:
    """杀掉占用 port 的【本网关】旧实例；陌生进程一律不动（返回 False，让调用方顺延端口）。"""
    pids = []
    if sys.platform == "darwin":
        try:
            out = subprocess.run(["lsof", "-ti", f"tcp:{port}", "-sTCP:LISTEN"],
                                 capture_output=True, text=True, timeout=10).stdout
            pids = [int(x) for x in out.splitlines() if x.strip().isdigit()]  # 一 pid 一行，整行纯数字才算
        except Exception:
            return False
    elif sys.platform != "win32":
        try:
            out = subprocess.run(["ss", "-ltnpH", f"sport = :{port}"],
                                 capture_output=True, text=True, timeout=10).stdout
            pids = [int(m) for m in re.findall(r"pid=(\d+)", out)]
        except Exception:
            return False
    else:
        try:
            out = subprocess.run(["netstat", "-ano", "-p", "TCP"],
                                 capture_output=True, text=True, timeout=10).stdout
            pids = [int(line.split()[-1]) for line in out.splitlines()
                    if f":{port}" in line and "LISTENING" in line
                    and line.split()[-1].isdigit()]
        except Exception:
            return False
    for pid in pids:
        if not _pid_is_gateway(pid):
            logger.warning("AUTO_KILL: 端口 %d 的监听 pid=%d 不是网关进程，不杀", port, pid)
            continue
        try:
            if sys.platform == "win32":
                subprocess.run(["taskkill", "/PID", str(pid), "/F"],
                               capture_output=True, text=True, timeout=10)
            else:
                os.kill(pid, 15)
            return True
        except Exception:
            continue
    return False


# ==== 单实例 + Linux SNI 托盘（模块级） ====

GUI = {"window": None, "quitting": False}
# 供 FastAPI 路由 / 第二实例激活使用；main() 内的 state 与它是同一个 dict。


def single_instance_lock_path() -> Path:
    """跨启动方式唯一的实例锁路径。
    不能放 DATA_DIR：源码跑（仓库目录）与打包跑（exe 目录）DATA_DIR 不同，
    各锁各的 → 双开。XDG_RUNTIME_DIR 是 per-user 会话目录，重启自动清理。"""
    if sys.platform == "linux":
        base = os.environ.get("XDG_RUNTIME_DIR") or f"/tmp/runtime-{os.getuid()}"
    else:
        import tempfile
        base = tempfile.gettempdir()
    path = Path(base) / "model-reservoir.lock"
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def image_to_argb(image) -> tuple:
    """PIL Image → (w, h, ARGB 大端字节)：SNI IconPixmap 载荷。纯函数可测。"""
    image = image.convert("RGBA")
    w, h = image.size
    raw = image.tobytes()
    out = bytearray(len(raw))
    out[0::4] = raw[3::4]  # A
    out[1::4] = raw[0::4]  # R
    out[2::4] = raw[1::4]  # G
    out[3::4] = raw[2::4]  # B
    return w, h, bytes(out)


def activate_running_instance(port: int) -> bool:
    """通知已在运行的实例把窗口带到前台（POST /api/activate，2 秒超时）。"""
    try:
        import urllib.request
        req = urllib.request.Request(
            f"http://127.0.0.1:{port}/api/activate",
            data=b"{}", headers={"Content-Type": "application/json"},
            method="POST")
        with urllib.request.urlopen(req, timeout=2) as resp:
            return resp.status == 200
    except Exception as e:
        logger.info("激活已有实例失败: %s", e)
        return False


def notify_running_instance(port: int) -> None:
    """激活失败时的兜底提醒（Linux notify-send，尽力而为，失败静默）。"""
    if sys.platform != "linux":
        return
    import shutil as _shutil
    if not _shutil.which("notify-send"):
        return
    try:
        subprocess.run(
            ["notify-send", "-a", "模型蓄水池", "模型蓄水池已在运行",
             f"已尝试唤起窗口（端口 {port}），也可从任务栏/托盘切回"],
            capture_output=True, timeout=3)
    except Exception:
        pass


def _niri_focus_our_window() -> None:
    """niri：按 pid 精确聚焦本进程窗口（跨 workspace 跳转）。独立线程，不卡主循环。"""
    import shutil as _shutil
    if not _shutil.which("niri"):
        return

    def _worker():
        try:
            out = subprocess.run(
                ["niri", "msg", "-j", "windows"],
                capture_output=True, timeout=2, text=True, check=True)
            for item in json.loads(out.stdout or "[]"):
                if item.get("pid") == os.getpid():
                    subprocess.run(
                        ["niri", "msg", "action", "focus-window",
                         "--id", str(item["id"])],
                        capture_output=True, timeout=2)
                    return
        except Exception as e:
            logger.debug("niri 聚焦失败: %s", e)

    threading.Thread(target=_worker, daemon=True).start()


def present_window_best_effort() -> bool:
    """把本实例窗口带到前台：niri IPC 按 pid 聚焦 + GTK present 兜底。
    可从任意线程调用（内部 idle_add 调度到主循环）。"""
    w = GUI.get("window")
    if w is None:
        return False

    def _do():
        try:
            w.show()
        except Exception as e:
            logger.debug("窗口 show 失败: %s", e)
        _niri_focus_our_window()
        try:
            import gi
            gi.require_version("Gtk", "3.0")
            from gi.repository import Gtk
            for tw in Gtk.Window.list_toplevels():
                if tw.get_visible():
                    tw.present()
        except Exception as e:
            logger.debug("GTK present 失败: %s", e)
        return False

    try:
        from gi.repository import GLib
        GLib.idle_add(_do)
        return True
    except Exception as e:
        logger.info("窗口唤起调度失败: %s", e)
        return False


def _quit_from_tray() -> None:
    """托盘菜单『退出』：置 quitting（放行关窗）并销毁窗口 → 进程退出。"""
    GUI["quitting"] = True
    w = GUI.get("window")
    if w is not None:
        try:
            w.destroy()
        except Exception as e:
            logger.debug("退出销毁窗口失败: %s", e)


def start_linux_tray(title: str, image) -> bool:
    """Linux 托盘：纯 DBus StatusNotifierItem + com.canonical.dbusmenu 右键菜单。
    不用 pystray——其 GTK 后端与 pywebview 抢 GLib 主循环（g_application_run
    CRITICAL → 历史 SIGSEGV）。DBusGMainLoop 把 method call 挂 default main
    context，由 pywebview 的 GTK 主循环统一 dispatch，无第二主循环。
    quickshell/DMS 官方支持 dbusmenu（commit 'add DBusMenu support'）。
    返回是否注册成功。"""
    try:
        import gi
        gi.require_version("GLib", "2.0")
        from gi.repository import GLib  # noqa: F401  (确保 loop 依赖就位)
        import dbus
        import dbus.service
        from dbus.mainloop.glib import DBusGMainLoop

        DBusGMainLoop(set_as_default=True)
        bus = dbus.SessionBus()
        own_name = f"org.kde.StatusNotifierItem-{os.getpid()}-1"
        bus.request_name(own_name)

        w_px, h_px, argb = image_to_argb(image)
        pixmap = dbus.Array(
            [dbus.Struct((w_px, h_px, dbus.ByteArray(argb)),
                         signature="(iiay)")],
            signature="(iiay)")

        class _SNI(dbus.service.Object):
            def __init__(self, bus, path):
                super().__init__(bus, path)

            @dbus.service.method("org.freedesktop.DBus.Properties",
                                 in_signature="ss", out_signature="v")
            def Get(self, interface, prop):
                return self._props()[prop]

            @dbus.service.method("org.freedesktop.DBus.Properties",
                                 in_signature="s", out_signature="a{sv}")
            def GetAll(self, interface):
                return self._props()

            def _props(self):
                return {
                    "Id": dbus.String("model-reservoir", variant_level=1),
                    "Category": dbus.String("ApplicationStatus", variant_level=1),
                    "Title": dbus.String(title, variant_level=1),
                    "Status": dbus.String("Active", variant_level=1),
                    "IconName": dbus.String("", variant_level=1),
                    "IconThemePath": dbus.String("", variant_level=1),
                    "IconPixmap": pixmap,
                    "Menu": dbus.ObjectPath("/MenuBar", variant_level=1),
                    "WindowId": dbus.UInt32(0, variant_level=1),
                    "ItemIsMenu": dbus.Boolean(False, variant_level=1),
                }

            @dbus.service.method("org.kde.StatusNotifierItem",
                                 in_signature="ii", out_signature="")
            def Activate(self, x, y):
                present_window_best_effort()

            @dbus.service.method("org.kde.StatusNotifierItem",
                                 in_signature="ii", out_signature="")
            def SecondaryActivate(self, x, y):
                present_window_best_effort()

            @dbus.service.method("org.kde.StatusNotifierItem",
                                 in_signature="ii", out_signature="")
            def ContextMenu(self, x, y):
                present_window_best_effort()

        class _DBusMenu(dbus.service.Object):
            def __init__(self, bus, path):
                super().__init__(bus, path)

            def _node(self, nid, props, children=()):
                kids = []
                for cid, cprops, cchildren in children:
                    kids.append(self._node(cid, cprops, cchildren))
                return dbus.Struct(
                    (nid,
                     dbus.Dictionary(props, signature="sv"),
                     dbus.Array(kids, signature="v")),
                    signature="ia{sv}av",
                    variant_level=1 if nid else 0)

            def _layout(self):
                menu = (
                    (1, {"label": dbus.String("显示窗口", variant_level=1),
                         "enabled": dbus.Boolean(True, variant_level=1)}, ()),
                    (2, {"type": dbus.String("separator", variant_level=1)}, ()),
                    (3, {"label": dbus.String("退出", variant_level=1),
                         "enabled": dbus.Boolean(True, variant_level=1)}, ()),
                )
                root = dbus.Struct(
                    (0,
                     dbus.Dictionary({"children-display":
                                      dbus.String("submenu", variant_level=1)},
                                     signature="sv"),
                     dbus.Array([self._node(*c) for c in menu],
                                signature="v")),
                    signature="ia{sv}av")
                return dbus.UInt32(1), root

            @dbus.service.method("com.canonical.dbusmenu",
                                 in_signature="iias", out_signature="u(ia{sv}av)")
            def GetLayout(self, parentId, recursionDepth, propertyNames):
                return self._layout()

            @dbus.service.method("com.canonical.dbusmenu",
                                 in_signature="ias", out_signature="a(ia{sv})")
            def GetGroupProperties(self, ids, propertyNames):
                props = {
                    0: {"children-display": "submenu"},
                    1: {"label": "显示窗口", "enabled": True},
                    2: {"type": "separator"},
                    3: {"label": "退出", "enabled": True},
                }
                out = []
                for i in ids:
                    i = int(i)
                    d = {}
                    for k, v in props.get(i, {}).items():
                        if isinstance(v, bool):
                            d[k] = dbus.Boolean(v, variant_level=1)
                        elif isinstance(v, str):
                            d[k] = dbus.String(v, variant_level=1)
                    out.append(dbus.Struct((i, dbus.Dictionary(d, signature="sv"))))
                return out

            @dbus.service.method("com.canonical.dbusmenu",
                                 in_signature="isvu", out_signature="")
            def Event(self, id, eventId, data, timestamp):
                if eventId == "clicked":
                    if id == 1:
                        present_window_best_effort()
                    elif id == 3:
                        _quit_from_tray()

            @dbus.service.method("com.canonical.dbusmenu",
                                 in_signature="a(isvu)", out_signature="ai")
            def EventGroup(self, events):
                return dbus.Array([], signature="i")

            @dbus.service.method("com.canonical.dbusmenu",
                                 in_signature="i", out_signature="b")
            def AboutToShow(self, id):
                return False

            @dbus.service.method("com.canonical.dbusmenu",
                                 in_signature="ai", out_signature="aiai")
            def AboutToShowGroup(self, ids):
                return [], []

            @dbus.service.method("org.freedesktop.DBus.Properties",
                                 in_signature="ss", out_signature="v")
            def Get(self, interface, prop):
                return self._menu_props()[prop]

            @dbus.service.method("org.freedesktop.DBus.Properties",
                                 in_signature="s", out_signature="a{sv}")
            def GetAll(self, interface):
                return self._menu_props()

            def _menu_props(self):
                return {
                    "Version": dbus.UInt32(4, variant_level=1),
                    "TextDirection": dbus.String("ltr", variant_level=1),
                    "Status": dbus.String("normal", variant_level=1),
                    "IconThemePath": dbus.Array([], signature="s",
                                                variant_level=1),
                }

        _SNI(bus, "/StatusNotifierItem")
        _DBusMenu(bus, "/MenuBar")
        watcher = bus.get_object("org.kde.StatusNotifierWatcher",
                                 "/StatusNotifierWatcher")
        watcher.RegisterStatusNotifierItem(own_name)
        return True
    except Exception as e:
        logger.warning("SNI 托盘不可用（%s）", e)
        return False

# ==== 单实例 + Linux SNI 托盘 结束 ====


if __name__ == "__main__":
    import uvicorn
    import time

    # ---- 注意：这里**故意不删** .old ----
    # 它是上一版的可执行文件，也是新版本「能启动但用不了」时唯一的手工退路。
    # 启动时删掉等于把退路提前烧了（误双击第二次也会把它删掉）。
    # .old 只在下一次更新替换时由 _do_swap_and_restart 清理，始终保留最近一份。

    # ---- 读取端口配置 ----
    cfg = load_config()
    desired_port = cfg.get("port", 8000)

    # ---- 端口工具函数 ----
    def port_in_use(port: int) -> bool:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            s.settimeout(0.5)
            return s.connect_ex(("127.0.0.1", port)) == 0

    def find_available_port(start: int, max_try: int = 100) -> int:
        for p in range(start, start + max_try):
            if not port_in_use(p):
                return p
        # 100 个端口全被占：宁可明确失败，也不返回一个还被占用的端口（会连到陌生服务上）
        raise RuntimeError(f"端口 {start}~{start + max_try - 1} 全部被占用，无法启动")

    # ---- 单实例限制（文件锁要最先，避免 config 被污染） ----
    # 锁放 XDG_RUNTIME_DIR：跨启动方式唯一（DATA_DIR 锁会让源码跑/打包跑
    # 各锁各的 → 双开）。GATEWAY_ALLOW_MULTI=1 放行多实例（多配置并存/测试）。
    AUTO_KILL = os.environ.get("GATEWAY_AUTO_KILL") == "1"
    ALLOW_MULTI = os.environ.get("GATEWAY_ALLOW_MULTI") == "1"

    # 如果是测试模式自动杀旧实例，先杀再拿锁
    if AUTO_KILL and port_in_use(desired_port):
        kill_old_instance(desired_port)
        time.sleep(1.5)

    _lock_fd = None
    if not ALLOW_MULTI:
        LOCK_FILE = str(single_instance_lock_path())
        try:
            _lock_fd = open(LOCK_FILE, "w")
            if sys.platform == "win32":
                import msvcrt
                msvcrt.locking(_lock_fd.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(_lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, IOError):
            logger.error("模型蓄水池已在运行中（锁 %s），尝试唤起已有窗口", LOCK_FILE)
            _activated = activate_running_instance(desired_port)
            if not _activated:
                notify_running_instance(desired_port)
                if sys.platform == "win32":
                    import ctypes
                    ctypes.windll.user32.MessageBoxW(
                        0, "模型蓄水池已在运行中，请勿重复启动。", "提示", 0x30
                    )
            sys.exit(0)

    # 自动找可用端口（锁拿到了才能安全改 config）
    actual_port = find_available_port(desired_port)
    if actual_port != desired_port:
        # 不写回配置：端口被占往往是临时的（别的程序正好在用），
        # 一旦写回就永久改掉，所有按 8000 集成的工具都会静默失效。
        logger.warning("端口 %d 被占用，本次临时使用 %d（配置仍是 %d）",
                       desired_port, actual_port, desired_port)

    # ---- FastAPI 服务器（daemon 线程） ----
    def start_server(port: int):
        uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")

    t = threading.Thread(target=start_server, args=(actual_port,), daemon=True)
    t.start()

    # ---- 轮询等待服务就绪（每 100ms 检查，最多等 5 秒） ----
    for _ in range(50):
        time.sleep(0.1)
        if port_in_use(actual_port):
            break

    url = f'http://127.0.0.1:{actual_port}/'

    # ---- GUI 尝试（可选）：headless 请求 或 GUI 依赖缺失时走浏览器模式 ----
    HEADLESS = (os.environ.get("GATEWAY_HEADLESS") == "1"
                or "--headless" in sys.argv)

    webview = None
    tray_icon = None
    state = GUI  # 模块级共享 dict：/api/activate 路由也读它

    if not HEADLESS:
        # Windows: WebView2 环境检测（Win7 等旧系统自动安装）
        if sys.platform == "win32":
            _webview2_ok = False
            try:
                import webview.platforms.edgechromium  # noqa: F401
                _webview2_ok = True
            except Exception:
                pass
            if not _webview2_ok:
                setup_exe = APP_DIR / "MicrosoftEdgeWebview2Setup.exe"
                if setup_exe.exists():
                    logger.info("WebView2 未安装，开始静默安装...")
                    try:
                        subprocess.run(
                            [str(setup_exe), "/silent", "/install"],
                            capture_output=True, timeout=120,
                        )
                        logger.info("WebView2 安装完成")
                    except Exception:
                        logger.warning("WebView2 安装失败，尝试使用系统默认浏览器")

        # ---- 窗口能力：webview 可导入才有 GUI ----
        try:
            import webview
        except Exception as e:
            logger.warning("webview 不可用（%s），回退浏览器/headless 模式", e)
            webview = None

        # ---- 托盘 ----
        # Linux 不能用 pystray（其 GTK 后端抢 GLib 主循环 → g_application_run
        # CRITICAL/历史 SIGSEGV）→ 走纯 DBus SNI（start_linux_tray）；
        # win/mac 仍用 pystray。Linux 默认开，GATEWAY_TRAY=0 关闭。
        tray_enabled = (sys.platform in ("win32", "darwin")
                        or os.environ.get("GATEWAY_TRAY") != "0")
        tray_icon = None
        if webview is None:
            pass
        elif not tray_enabled:
            logger.info("托盘已按配置关闭（GATEWAY_TRAY=0），窗口关闭即退出")
        else:
            # ---- 生成托盘图标（三端共用）----
            def create_tray_icon():
                from PIL import Image, ImageDraw
                img = Image.new('RGBA', (64, 64), (0, 0, 0, 0))
                draw = ImageDraw.Draw(img)
                draw.rounded_rectangle([4, 4, 60, 60], radius=14,
                                       fill=(30, 144, 255))
                draw.polygon([(22, 20), (44, 32), (22, 44)], fill="white")
                return img

            if sys.platform.startswith("linux"):
                if start_linux_tray("模型蓄水池", create_tray_icon()):
                    # SNI 注册是同步完成的；truthy 标记让 on_closing 走"隐藏到托盘"
                    tray_icon = True
                    logger.info("SNI 托盘已注册（左键=显示窗口，右键菜单=显示窗口/退出）")
                else:
                    logger.info("SNI 托盘不可用，窗口关闭即退出")
            else:
                try:
                    import pystray

                    def on_show(icon, item):
                        w = state["window"]
                        if w:
                            w.show()

                    def on_quit(icon, item):
                        state["quitting"] = True
                        icon.stop()
                        w = state["window"]
                        if w:
                            w.destroy()

                    tray_icon = pystray.Icon(
                        "model-gateway",
                        create_tray_icon(),
                        "模型蓄水池",
                        menu=pystray.Menu(
                            pystray.MenuItem("显示窗口", on_show, default=True),
                            pystray.MenuItem("退出", on_quit),
                        ),
                    )
                except Exception as e:
                    logger.warning("托盘不可用（%s），以纯窗口模式运行", e)
                    tray_icon = None

    if webview is None:
        # ---- headless：只跑服务，用浏览器访问 ----
        logger.info("Web UI: %s", url)
        print(f"模型蓄水池已启动: {url}")
        if os.environ.get("GATEWAY_NO_BROWSER") != "1":
            try:
                webbrowser.open(url)
            except Exception:
                pass
        while True:
            time.sleep(3600)

    # ---- 创建窗口并直接加载页面 ----
    window = webview.create_window(
        '模型蓄水池', url, width=1200, height=800
    )
    state["window"] = window

    def on_closing():
        if state["quitting"] or tray_icon is None:
            # 正在退出 / 无托盘可藏：放行关闭 → webview.start() 返回 → 进程退出
            return True
        # 有托盘：拦截关闭事件，隐藏到托盘常驻
        window.hide()
        return False

    window.events.closing += on_closing

    # ---- 启动系统托盘（有才启） ----
    if tray_icon is not None and hasattr(tray_icon, "run"):
        threading.Thread(target=tray_icon.run, daemon=True).start()

    # ---- 启动 webview ----
    # private_mode 是 pywebview5 的默认值，但会禁用 localStorage/cookies
    # （gtk.py: enable_html5_local_storage=False）→ 主题偏好、页面自诊断全挂，
    # 表现为"点主题按钮没反应/不跟随系统"。本应用需要持久化 UI 偏好 → 关掉。
    try:
        webview.start(private_mode=False)
    except Exception as e:
        # WebView2 没装 / WebKitGTK 缺库时，pywebview 会在 start() 里抛错，
        # 甚至直接让进程退出 —— 用户只看到窗口一闪就没，服务也没了。
        # 这里退化成浏览器模式：服务已经在跑，把页面丢给系统浏览器。
        logger.exception("GUI 启动失败，回退到浏览器模式：%s", e)
        print(f"GUI 启动失败（{e}），已切换为浏览器模式：{url}")
        if os.environ.get("GATEWAY_NO_BROWSER") != "1":
            try:
                webbrowser.open(url)
            except Exception:
                pass
        while True:
            time.sleep(3600)

