#!/usr/bin/env python3
"""组队系统的客户端侧（跑在用户本机的网关里）

职责：
- 本地身份：首次入队时生成 UUID（不冲突），队伍内昵称可改
- 队长：建队拿 6 位码 → 逐个上游勾选「允许同步给队员」→ 加密上传
- 队员：输码入队 → 等审批 → 轮询拉密文包 → 解密写入自己的 providers.json
- 关队/被踢：把对应队伍来的 provider 标记为失效（置灰、模型全禁用），记录保留可自行删除

安全边界（用户已选定 B 同步型 + 默认不同步）：
- **密钥一旦复制到队员机器就无法真正收回**。队长能做的是：让队员侧记录失效 +
  提醒队员可自行删除 + 自己去上游换 key。界面上必须明确警告。
- 默认不勾选 = 什么都不下发；勾选哪个上游，哪个才被复制。
"""
from __future__ import annotations

import json
import secrets
import sys
import time
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "team"))
from protocol import (                       # noqa: E402
    code_hash, derive_team_key, encrypt_for_member, decrypt_as_member,
)

TEAM_FILE_NAME = "team.json"
DEFAULT_SERVER = "https://model-reservoir.heilongxiake.cn"
POLL_INTERVAL = 30          # 秒
# provider 上带这两个字段标记"来自队伍"，便于置灰与统计
TEAM_ORIGIN_KEY = "team_origin"
TEAM_STATUS_KEY = "team_status"


class TeamState:
    """本地组队状态（team.json）。线程安全靠 asyncio 单线程事件循环。"""

    def __init__(self, data_dir: Path):
        self.path = Path(data_dir) / TEAM_FILE_NAME
        self._data: dict = {}
        self.load()

    # ---------- 持久化 ----------
    def load(self):
        try:
            if self.path.exists():
                self._data = json.loads(self.path.read_text(encoding="utf-8"))
        except Exception:
            self._data = {}
        if not isinstance(self._data, dict):
            self._data = {}
        self._data.setdefault("client_id", "")
        self._data.setdefault("server", DEFAULT_SERVER)
        self._data.setdefault("teams", {})

    def save(self):
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self._data, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    # ---------- 本地身份 ----------
    @property
    def client_id(self) -> str:
        if not self._data.get("client_id"):
            self._data["client_id"] = str(uuid.uuid4())
            self.save()
        return self._data["client_id"]

    @property
    def server(self) -> str:
        return self._data.get("server") or DEFAULT_SERVER

    def set_server(self, url: str):
        self._data["server"] = (url or DEFAULT_SERVER).rstrip("/")
        self.save()

    # ---------- 队伍 ----------
    def get_team(self, team_id: str) -> dict | None:
        return self._data.get("teams", {}).get(team_id)

    def put_team(self, team_id: str, info: dict):
        self._data.setdefault("teams", {})[team_id] = info
        self.save()

    def drop_team(self, team_id: str):
        self._data.get("teams", {}).pop(team_id, None)
        self.save()

    def list_teams(self) -> dict:
        return self._data.get("teams", {})


# ============================================================
# 与协调服务通信（全部走 HTTPS，失败不抛到调用方）
# ============================================================
async def _req(state: TeamState, method: str, path: str, body: dict | None = None,
               token: str | None = None, timeout: float = 20.0):
    import httpx
    url = state.server + path
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = "Bearer " + token
    try:
        async with httpx.AsyncClient(timeout=timeout, trust_env=False) as c:
            r = await c.request(method, url, json=body, headers=headers)
            try:
                data = r.json()
            except Exception:
                data = {"ok": False, "error": "bad_json", "status": r.status_code}
            data.setdefault("_http", r.status_code)
            return data
    except Exception as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "_http": 0}


# ============================================================
# 队长侧
# ============================================================
async def create_team(state: TeamState, nickname: str = "队长", ttl: int = 7 * 86400,
                      code_len: int = 6) -> dict:
    """建队。返回 {team_id, code, ...}。码只在本机明文持有，服务端只见摘要。"""
    if code_len == 6:
        code = "%06d" % (secrets.randbelow(900000) + 100000)
    else:                                   # 8 位大熵码（更高安全档）
        alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # 去掉易混的 0/O/1/I
        code = "".join(secrets.choice(alphabet) for _ in range(8))
    salt = secrets.token_hex(32)
    d = await _req(state, "POST", "/api/teams", {
        "captain_client_id": state.client_id,
        "code_hash": code_hash(code, salt),
        "code_salt": salt,
        "nickname": nickname or "队长",
        "ttl": ttl,
    })
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error", "建队失败"), "http": d.get("_http")}
    team_id_local = d["team_id"]
    state.put_team(team_id_local, {
        "role": "captain", "token": d["token"], "code": code, "code_salt": salt,
        "nickname": nickname or "队长", "status": "active",
        "created_at": time.time(), "expires_at": d.get("expires_at", time.time() + ttl),
    })
    return {"ok": True, "team_id": team_id_local, "code": code,
            "expires_at": d.get("expires_at"), "server": state.server}


async def approve_member(state: TeamState, team_id: str, member_client_id: str) -> dict:
    t = state.get_team(team_id)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "不是队长"}
    d = await _req(state, "POST", f"/api/teams/{team_id}/approve",
                   {"client_id": member_client_id}, t["token"])
    return {"ok": bool(d.get("ok")), "members": d.get("members", []), "error": d.get("error")}


async def kick_member(state: TeamState, team_id: str, member_client_id: str) -> dict:
    t = state.get_team(team_id)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "不是队长"}
    d = await _req(state, "POST", f"/api/teams/{team_id}/kick",
                   {"client_id": member_client_id}, t["token"])
    return {"ok": bool(d.get("ok")), "members": d.get("members", []), "error": d.get("error")}


async def close_team(state: TeamState, team_id: str) -> dict:
    t = state.get_team(team_id)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "不是队长"}
    d = await _req(state, "POST", f"/api/teams/{team_id}/close", {}, t["token"])
    if d.get("ok"):
        # 关队后本地队伍记录保留但置失效；队伍带来的 provider 由 mark_team_dead 统一处理
        t["status"] = "closed"
        state.put_team(team_id, t)
    return {"ok": bool(d.get("ok")), "error": d.get("error")}


async def sync_provider_to_member(state: TeamState, team_id: str,
                                  member_client_id: str, provider: dict) -> dict:
    """把一个 provider（含 api_key）加密后推给指定队员。这是唯一的密钥外流通道。"""
    t = state.get_team(team_id)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "不是队长"}
    team_key = derive_team_key(t["code"], team_id, t["code_salt"])
    payload = json.dumps(provider, ensure_ascii=False).encode("utf-8")
    nonce, ct = encrypt_for_member(team_key, member_client_id, payload)
    d = await _req(state, "POST", f"/api/teams/{team_id}/packages", {
        "target_client_id": member_client_id,
        "provider_name": provider.get("name", "")[:64],
        "nonce": nonce.hex(), "payload": ct.hex(),
    }, t["token"])
    return {"ok": bool(d.get("ok")), "error": d.get("error")}


# ============================================================
# 队员侧
# ============================================================
async def join_team(state: TeamState, team_id: str, code: str, nickname: str = "队员") -> dict:
    d = await _req(state, "POST", "/api/teams/join", {
        "code": code, "team_id": team_id,
        "client_id": state.client_id, "nickname": nickname or "队员",
    })
    if not d.get("ok"):
        return {"ok": False, "error": d.get("error", "入队失败"), "http": d.get("_http")}
    state.put_team(team_id, {
        "role": "member", "token": d["token"], "code": code,
        "code_salt": d.get("code_salt", ""), "nickname": nickname or "队员",
        "status": d.get("status", "pending"), "joined_at": time.time(),
    })
    return {"ok": True, "team_id": team_id, "status": d.get("status"),
            "members": d.get("members", [])}


async def fetch_team_state(state: TeamState, team_id: str) -> dict:
    t = state.get_team(team_id)
    if not t:
        return {"ok": False, "error": "本地无此队伍"}
    return await _req(state, "GET", f"/api/teams/{team_id}/state", token=t["token"])


async def pull_packages(state: TeamState, team_id: str) -> list[dict]:
    """拉本队发给我的密文包并解密。返回 provider 字典列表。"""
    t = state.get_team(team_id)
    if not t or not t.get("code_salt"):
        return []
    d = await _req(state, "GET", f"/api/teams/{team_id}/packages", token=t["token"])
    if not d.get("ok"):
        return []
    out = []
    team_key = derive_team_key(t["code"], team_id, t["code_salt"])
    for p in d.get("packages", []):
        try:
            raw = decrypt_as_member(team_key, state.client_id,
                                    bytes.fromhex(p["nonce"]), bytes.fromhex(p["payload"]))
            prov = json.loads(raw.decode("utf-8"))
            if isinstance(prov, dict):
                prov.setdefault("name", p.get("provider_name", "来自队伍的上游"))
                out.append(prov)
        except Exception:
            # 解不开通常是 code 已换（队长 rotate 过）或非本人的包
            continue
    return out


# ============================================================
# 本地 provider 标记：置灰 / 失效
# ============================================================
def _provider_team_id(p: dict):
    return (p.get(TEAM_ORIGIN_KEY) or {}).get("team_id")


def mark_team_dead(providers: list, team_id: str, reason: str = "dissolved") -> tuple[list, int]:
    """把某队伍同步来的 provider 标记失效（置灰 + 全禁用），记录保留可自行删除。"""
    n = 0
    for p in providers:
        if _provider_team_id(p) == team_id and p.get(TEAM_STATUS_KEY) != reason:
            p[TEAM_STATUS_KEY] = reason
            # 禁用其所有模型，避免被路由选中（用户仍可看到记录）
            p.setdefault("disabled_models", [])
            for m in p.get("models", []):
                if m not in p["disabled_models"]:
                    p["disabled_models"].append(m)
            n += 1
    return providers, n


def merge_team_provider(providers: list, incoming: dict, team_id: str,
                        captain_name: str = "") -> tuple[list, bool, str]:
    """把解密来的 provider 并入本地列表。返回 (列表, 是否新增/更新, 说明)。

    - 重名不同源 → 加后缀，避免覆盖用户自己的同名上游
    - 已存在同队同名 → 更新（队长重新推送）
    - 已存在完全相同的 base_url + api_key → 认为是同一个，直接更新
    """
    name = incoming.get("name") or "来自队伍的上游"
    base_url = (incoming.get("base_url") or "").rstrip("/")
    key = incoming.get("api_key") or ""
    incoming[TEAM_ORIGIN_KEY] = {
        "team_id": team_id, "captain": captain_name, "synced_at": time.time(),
    }
    incoming[TEAM_STATUS_KEY] = "active"

    # 同队同名 → 更新
    for i, p in enumerate(providers):
        if _provider_team_id(p) == team_id and p.get("name") == name:
            providers[i] = incoming
            return providers, True, f"更新「{name}」"

    # 同 base_url+key（不管来源）→ 认为同一个
    for i, p in enumerate(providers):
        if ((p.get("base_url") or "").rstrip("/") == base_url
                and (p.get("api_key") or "") == key and key):
            incoming["name"] = p.get("name", name)
            providers[i] = incoming
            return providers, True, f"更新已有上游「{incoming['name']}」"

    # 与本地自有上游重名 → 加后缀
    existing_names = {p.get("name") for p in providers}
    if name in existing_names:
        suffix = f"·队伍-{(captain_name or team_id)[:8]}"
        new_name, k = name, 1
        while new_name in existing_names:
            new_name = f"{name}{suffix}{'' if k == 1 else k}"
            k += 1
        incoming["name"] = new_name
        name = new_name

    providers.append(incoming)
    return providers, True, f"新增「{name}」"


def team_providers_summary(providers: list) -> dict:
    """给前端用：按队伍分组统计，以及失效数量。"""
    by_team: dict[str, dict] = {}
    dead = 0
    for p in providers:
        tid = _provider_team_id(p)
        if not tid:
            continue
        st = p.get(TEAM_STATUS_KEY, "active")
        e = by_team.setdefault(tid, {"active": 0, "dissolved": 0, "kicked": 0,
                                     "names": [], "captain": ""})
        e[st] = e.get(st, 0) + 1
        e["names"].append(p.get("name"))
        e["captain"] = (p.get(TEAM_ORIGIN_KEY) or {}).get("captain", "")
        if st != "active":
            dead += 1
    return {"by_team": by_team, "dead": dead}


async def pull_packages_on_all(state: TeamState, providers: list, save_providers,
                                lock) -> dict:
    """后台轮询用：对每个队伍拉包并合并；队伍已死/被踢则置失效。

    providers 是**就地修改**的列表（app.py 传的是模块级引用）。
    返回 {imported: [...], marked_dead: n}。
    """
    imported, dead = [], 0
    for one in list(state.list_teams().keys()):
        t = state.get_team(one)
        if not t:
            continue
        remote = await fetch_team_state(state, one)
        if remote.get("ok"):
            me_status = (remote.get("me") or {}).get("status")
            if not remote.get("alive") or me_status == "kicked":
                async with lock:
                    reason = "kicked" if me_status == "kicked" else "dissolved"
                    providers[:], n = mark_team_dead(providers, one, reason)
                    if n:
                        save_providers(providers)
                    dead += n
                if t.get("status") not in ("kicked", "closed"):
                    t["status"] = "kicked" if me_status == "kicked" else "closed"
                    state.put_team(one, t)
                continue
            if me_status == "approved" and t.get("status") != "approved":
                t["status"] = "approved"
                state.put_team(one, t)
        provs = await pull_packages(state, one)
        if not provs:
            continue
        # 队长昵称从远端成员表里找（本地 t.nickname 是我自己的昵称，不是队长的）
        captain_name = ""
        if remote.get("ok"):
            for m in remote.get("members", []):
                if m.get("role") == "captain":
                    captain_name = m.get("nickname") or ""
                    break
        async with lock:
            changed = False
            for p in provs:
                providers[:], added, _note = merge_team_provider(
                    providers, p, one, captain_name)
                if added:
                    imported.append(p.get("name"))
                    changed = True
            if changed:
                save_providers(providers)
    return {"imported": imported, "marked_dead": dead}


# ============================================================
# 用量上报（队员侧）：只上报数字，绝不上报对话内容
# ============================================================
def _usage_cursor_path(data_dir: Path) -> Path:
    return Path(data_dir) / "team_usage_cursor.json"


def _pending_usage(data_dir: Path, cursor: dict) -> tuple[list[dict], dict]:
    """从 usage.jsonl 里取未上报的记录。cursor 存 {文件inode: 已读字节偏移}。"""
    uf = Path(data_dir) / "usage.jsonl"
    if not uf.exists():
        return [], cursor
    try:
        ino = str(uf.stat().st_ino)
    except OSError:
        return [], cursor
    offset = int(cursor.get(ino, 0))
    size = uf.stat().st_size
    if size < offset:                       # 文件被截断/轮转 → 重头来
        offset = 0
    items = []
    with open(uf, "r", encoding="utf-8") as f:
        f.seek(offset)
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except Exception:
                continue
            items.append({
                "ts": rec.get("ts", time.time()),
                "provider": str(rec.get("provider", ""))[:64],
                "model": str(rec.get("model", ""))[:128],
                "pt": int(rec.get("pt") or 0),
                "ct": int(rec.get("ct") or 0),
                "tt": int(rec.get("tt") or 0),
            })
        new_offset = f.tell()
    cursor[ino] = new_offset
    return items, cursor


async def report_usage(state: TeamState, data_dir: Path) -> dict:
    """把自己网关的用量上报到所有我所在的队伍（作为队员时才报）。"""
    cursor = {}
    cp = _usage_cursor_path(data_dir)
    try:
        if cp.exists():
            cursor = json.loads(cp.read_text(encoding="utf-8"))
    except Exception:
        cursor = {}
    items, cursor = _pending_usage(data_dir, cursor)
    try:
        cp.write_text(json.dumps(cursor), encoding="utf-8")
    except Exception:
        pass
    if not items:
        return {"ok": True, "reported": 0}

    # 分批，每批不超过 200 条（服务端上限）
    reported = 0
    ok_all = True
    for team_id, t in state.list_teams().items():
        if t.get("role") != "member" or t.get("status") not in ("approved", "pending"):
            continue
        for i in range(0, len(items), 200):
            batch = items[i:i + 200]
            d = await _req(state, "POST", f"/api/teams/{team_id}/usage",
                           {"items": batch}, t["token"])
            if d.get("ok"):
                reported += int(d.get("accepted") or 0)
            else:
                ok_all = False
    return {"ok": ok_all, "reported": reported, "pending": len(items)}


async def fetch_usage_stats(state: TeamState, team_id: str) -> dict:
    """队长拉分人用量。"""
    t = state.get_team(team_id)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "只有队长能看队员用量"}
    return await _req(state, "GET", f"/api/teams/{team_id}/usage", token=t["token"])


async def set_nickname(state: TeamState, team_id: str, nickname: str) -> dict:
    t = state.get_team(team_id)
    if not t:
        return {"ok": False, "error": "本地无此队伍"}
    d = await _req(state, "POST", f"/api/teams/{team_id}/nickname",
                   {"nickname": nickname}, t["token"])
    if d.get("ok"):
        t["nickname"] = nickname
        state.put_team(team_id, t)
    return {"ok": bool(d.get("ok")), "members": d.get("members", []), "error": d.get("error")}


async def leave_team(state: TeamState, team_id: str) -> dict:
    """本地离开队伍：删本地记录；队伍来的 provider 置为失效（不删，用户可自行删）。"""
    t = state.get_team(team_id)
    if not t:
        return {"ok": False, "error": "本地无此队伍"}
    state.drop_team(team_id)
    return {"ok": True}


__all__ = [
    "TeamState", "DEFAULT_SERVER", "TEAM_ORIGIN_KEY", "TEAM_STATUS_KEY",
    "create_team", "approve_member", "kick_member", "close_team",
    "sync_provider_to_member", "join_team", "fetch_team_state", "pull_packages",
    "mark_team_dead", "merge_team_provider", "team_providers_summary",
    "pull_packages_on_all",
    "report_usage", "fetch_usage_stats", "set_nickname", "leave_team",
]
