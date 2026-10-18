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
import time
import uuid
from pathlib import Path

# protocol.py 与 app.py 同在项目根目录 —— 这样 PyInstaller 能静态发现并打进包。
# 以前放在 team/ 子目录 + sys.path.insert 动态找，源码模式能跑但打包版必崩
# （冻结环境里没有 team/ 子目录，且动态路径 PyInstaller 追踪不到）。
from protocol import (                       # noqa: E402
    code_hash, derive_team_key, encrypt_for_member, decrypt_as_member,
    encrypt_broadcast, decrypt_broadcast,
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
        # 这个文件里有组队码与成员 token —— 明文，能直接派生 K_team。
        # 默认 umask 写出来是 0644，同机低权账户可读。改成仅属主可读写。
        try:
            tmp.chmod(0o600)
        except OSError:
            pass            # 某些文件系统不支持 chmod，不因此让保存失败
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
def gen_code(code_len: int = 8) -> str:
    """生成入队码。6 位数字是低安全档，8 位大熵码才是默认。"""
    if code_len == 6:
        return "%06d" % (secrets.randbelow(900000) + 100000)
    alphabet = "23456789ABCDEFGHJKLMNPQRSTUVWXYZ"   # 去掉易混的 0/O/1/I
    return "".join(secrets.choice(alphabet) for _ in range(8))


async def create_team(state: TeamState, nickname: str = "队长", ttl: int = 7 * 86400,
                      code_len: int = 8) -> dict:
    """建队。返回 {team_id, code, ...}。码只在本机明文持有，服务端只见摘要。

    code_len=8 是默认：6 位数字只有 20 bit 熵，协调服务的数据库一旦泄漏，
    离线枚举 100 万个码约 19 核·小时/队（scrypt N=2^15 实测 67.6ms/次），
    租几十个 vCPU 几十分钟就能解出全部成员的密钥 —— 协议的核心威胁模型被击穿。
    8 位 32 字母表约 2^40，同量级算力要几十年。6 位只作为低安全档保留。
    """
    code = gen_code(code_len)
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
async def rotate_code(state: TeamState, team_id: str, code_len: int = 8) -> dict:
    """轮换入队码。永久队伍尤其需要 —— 码一旦泄漏就是永久开放。

    注意：换码会换掉 K_team（它由 code 派生），所以**换码后**
    队里已有的密文包全体失效，成员需要重新拉取。已在机器上的上游
    不受影响（密钥已经落地），但之后的新贡献要用新码才能互解。
    """
    t = state.get_team(team_id)
    if not t or t.get("role") != "captain":
        return {"ok": False, "error": "只有队长能轮换组队码"}
    code = gen_code(code_len)
    salt = secrets.token_hex(32)
    d = await _req(state, "POST", f"/api/teams/{team_id}/rotate-code",
                   {"code_hash": code_hash(code, salt), "code_salt": salt},
                   token=t["token"])
    if not d.get("ok"):
        return d
    t["code"] = code
    t["code_salt"] = salt
    state.put_team(team_id, t)
    return {"ok": True, "code": code, "code_len": code_len,
            "warning": "换码后旧码立即失效；已在队员机器上的密钥收不回来。"}


async def join_team(state: TeamState, team_id: str, code: str, nickname: str = "队员") -> dict:
    d = await _req(state, "POST", "/api/teams/join", {
        "code": code, "team_id": team_id,
        "client_id": state.client_id, "nickname": nickname or "队员",
    })
    if not d.get("ok"):
        # 服务端把「已存在的 client_id 再次 join」判为 409 —— 这是安全设计：
        # 不允许重复 join，否则知道组队码的人可用队长的 client_id 冒名顶替。
        # 已成员应该用本地 team.json 里已有的 token，而不是重新入队。
        err = d.get("error") or "入队失败"
        shown = {
            "already_member": "你已经是这个队伍的成员了，无需重复入队",
            "team_full": "队伍已满",
            "kicked": "你已被队长移出该队伍",
            "wrong_code": "组队码不对",
            "too_many_attempts": "错码次数过多，请稍后再试",
            "team_unavailable": "队伍已关闭或已过期",
        }.get(err, err)
        return {"ok": False, "error": shown, "http": d.get("_http"), "code": err}
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
        raw = None
        try:
            # 先试广播通道（K_team，全队可见）——共享池里绝大多数包是这种
            raw = decrypt_broadcast(team_key,
                                    bytes.fromhex(p["nonce"]), bytes.fromhex(p["payload"]))
        except Exception:
            try:
                # 再试定向通道（K_member）——队长单独发给我的
                raw = decrypt_as_member(team_key, state.client_id,
                                        bytes.fromhex(p["nonce"]), bytes.fromhex(p["payload"]))
            except Exception:
                # 解不开通常是 code 已换（队长 rotate 过）或不是给我的包
                continue
        try:
            prov = json.loads(raw.decode("utf-8"))
        except Exception:
            continue
        if isinstance(prov, dict):
            prov.setdefault("name", p.get("provider_name", "来自队伍的上游"))
            prov["_contributor"] = p.get("contributor_client_id") or ""
            out.append(prov)
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
                        captain_name: str = "",
                        contributor: str = "") -> tuple[list, bool, str]:
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

    # 同队同名：只有 key 也相同才算「同一条的更新」（队长改了设置重新推送）。
    # key 不同说明是**另一个账号**，必须并存 —— 团队里每人各自注册免费账号，
    # 同名不同 key 若覆盖，等于把别人的额度顶掉，正好违背组队的初衷。
    for i, p in enumerate(providers):
        if _provider_team_id(p) == team_id and p.get("name") == name:
            if (p.get("api_key") or "") == key:
                providers[i] = incoming
                return providers, True, f"更新「{name}」"
            break   # 名字冲突但 key 不同 → 落到下面的加后缀分支并存

    # 同 base_url+key（不管来源）→ 认为同一个
    for i, p in enumerate(providers):
        if ((p.get("base_url") or "").rstrip("/") == base_url
                and (p.get("api_key") or "") == key and key):
            incoming["name"] = p.get("name", name)
            providers[i] = incoming
            return providers, True, f"更新已有上游「{incoming['name']}」"

    # 与本地自有上游重名，或同队同名但 key 不同 → 加后缀并存。
    # 后缀优先用**贡献者**：共享池里 14 个人各贡献一条同名上游，
    # 只写「队伍」根本分不清谁是谁。
    existing_names = {p.get("name") for p in providers}
    if name in existing_names:
        who = contributor or captain_name or team_id
        suffix = f"·{who[:8]}"
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


def _contributable(providers: list, team_id: str) -> list[dict]:
    """挑出该广播给全队的本地自有上游。

    - 来自队伍的不回传（否则队里会无限回声，同一条上游被反复转发）
    - 已经被队长关掉同步的平台不传（白名单，opt-out）
    """
    out = []
    for p in providers:
        if _provider_team_id(p):
            continue                     # 来自队伍的，不回传
        out.append(p)
    return out


async def push_contributions(state: TeamState, providers: list,
                             allow: dict | None = None) -> list[str]:
    """把自己本地自有的上游广播给全队。返回本次上传的名字列表。

    白名单 `allow` 是 {平台名: bool}，**默认全开**（用户定的：
    默认都是同步的，只有队长显式关掉才不同步）。白名单不过的绝不能
    进广播通道 —— 广播意味着全队可见。
    """
    allow = allow or {}
    t = None
    uploaded = []
    for team_id in list(state.list_teams().keys()):
        t = state.get_team(team_id)
        if not t or t.get("status") != "approved" or not t.get("code_salt"):
            continue
        team_key = derive_team_key(t["code"], team_id, t["code_salt"])
        for p in _contributable(providers, team_id):
            name = p.get("name") or ""
            if allow.get(name, True) is False:
                continue
            blob = {k: v for k, v in p.items() if not k.startswith("team_")}
            nonce, ct = encrypt_broadcast(
                team_key, json.dumps(blob, ensure_ascii=False).encode("utf-8"))
            d = await _req(state, "POST", f"/api/teams/{team_id}/packages",
                           body={"provider_name": name, "target_client_id": "",
                                 "nonce": nonce.hex(), "payload": ct.hex()},
                           token=t["token"])
            if d.get("ok"):
                uploaded.append(name)
    return uploaded


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
        # 先把自己的贡献推上去（共享池：双向）。失败不影响后续拉取。
        try:
            allow = t.get("sync_allow") or {}
            await push_contributions(state, providers, allow)
        except Exception:
            pass
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
                    providers, p, one, captain_name,
                    contributor=p.pop("_contributor", ""))
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


def _save_cursor(cp: Path, cursor: dict):
    """原子写用量游标。游标里只有字节数与 inode，不敏感，但顺手限权限。"""
    try:
        tmp = cp.with_suffix(cp.suffix + ".tmp")
        tmp.write_text(json.dumps(cursor), encoding="utf-8")
        try:
            tmp.chmod(0o600)
        except OSError:
            pass
        tmp.replace(cp)
    except Exception:
        pass                # 游标写失败只影响下轮重读，不能让它打断上报


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
    items, new_cursor = _pending_usage(data_dir, cursor)
    if not items:
        # 没有新记录也要落盘（游标可能因文件变短而回退过）
        _save_cursor(cp, new_cursor)
        return {"ok": True, "reported": 0}

    # 分批，每批不超过 200 条（服务端上限）
    #
    # 游标必须在上报**成功之后**才推进。以前是读完就写游标，导致服务端
    # 不可达/超时（_req 兜底返回 ok:false）时这批记录永远不再读 —— 静默丢报。
    # 反过来「先成功再推进」最多在崩溃时少量重复，而服务端可按
    # client_id+ts+provider+model 去重，重复远比丢失可接受。
    reported = 0
    ok_all = True
    any_success = False
    for team_id, t in state.list_teams().items():
        if t.get("role") != "member" or t.get("status") not in ("approved", "pending"):
            continue
        for i in range(0, len(items), 200):
            batch = items[i:i + 200]
            d = await _req(state, "POST", f"/api/teams/{team_id}/usage",
                           {"items": batch}, t["token"])
            if d.get("ok"):
                reported += int(d.get("accepted") or 0)
                any_success = True
            else:
                ok_all = False
    # 至少一个队伍上报成功，就认为这批已被接收，推进游标。
    # 全部失败则不推进，下一轮重试同一批（宁可重复不可丢失）。
    if any_success:
        _save_cursor(cp, new_cursor)
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
