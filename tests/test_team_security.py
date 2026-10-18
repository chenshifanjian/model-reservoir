#!/usr/bin/env python3
"""组队协调服务的安全回归测试。

覆盖 2026-10-18 审查发现并修复的 6 个问题。最重要的一个是 HIGH：

    join 曾允许已存在的 client_id 重新认证，并把 role 硬编码成 "member"、
    换掉 token。而 client_id 是公开的（list_members 返回给每个成员）、
    join 只验证组队码（码恰恰要分享给队员）→ 任何队员都能拿队长的 client_id
    冒名顶替，把队长降级并夺走其 token，队长被踢出自己的队伍。

修法：join 只做「新成员加入」，已存在成员返回 409 already_member。
"""
import json
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
import protocol  # noqa: E402

CODE = "482913"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class Api:
    def __init__(self, base: str):
        self.base = base

    def post(self, path, obj=None, token=None):
        headers = {"Content-Type": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        req = urllib.request.Request(self.base + path, data=json.dumps(obj or {}).encode(),
                                     headers=headers, method="POST")
        try:
            r = urllib.request.urlopen(req, timeout=15)
            return r.getcode(), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")

    def get(self, path, token):
        req = urllib.request.Request(self.base + path,
                                     headers={"Authorization": "Bearer " + token})
        try:
            r = urllib.request.urlopen(req, timeout=15)
            return r.getcode(), json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read() or b"{}")


@pytest.fixture()
def srv(tmp_path_factory):
    """起一个临时协调服务（独立端口/独立库，不碰任何线上实例）。

    必须是 function 级：建队限流按 IP 计数，module 级共享服务会让
    test_create_rate_limit 耗光配额、连累后面的测试建队 429。
    """
    d = tmp_path_factory.mktemp("coord")
    db = d / "t.db"
    port = _free_port()
    root = Path(__file__).resolve().parent.parent
    p = subprocess.Popen([sys.executable, str(root / "team" / "coordinator.py"),
                          str(db), str(port)],
                         stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    base = f"http://127.0.0.1:{port}"
    for _ in range(60):
        try:
            urllib.request.urlopen(base + "/health", timeout=2)
            break
        except Exception:
            time.sleep(0.25)
    else:  # pragma: no cover
        p.terminate()
        pytest.fail("协调服务未就绪")
    yield Api(base), str(db)
    p.terminate()
    p.wait()


def _mkteam(api: Api, cid: str, nick="队长", ttl=3600):
    import secrets
    salt = secrets.token_bytes(32).hex()
    code, body = api.post("/api/teams", {
        "captain_client_id": cid, "code_hash": protocol.code_hash(CODE, salt),
        "code_salt": salt, "ttl": ttl, "nickname": nick})
    assert code == 200, f"建队失败 HTTP={code} {body}"
    return body["team_id"], body["token"]


def test_cannot_impersonate_captain(srv):
    """核心安全回归：知道组队码的队员不能顶替队长。"""
    api, _ = srv
    tid, cap_tok = _mkteam(api, "cap-A", "真队长")

    # 冒用队长 client_id 再次 join
    c, b = api.post("/api/teams/join", {"team_id": tid, "code": CODE,
                                        "client_id": "cap-A", "nickname": "冒名者"})
    assert c == 409, f"应拒绝已存在成员，实际 HTTP={c}"
    assert b.get("error") == "already_member"
    assert not b.get("token"), "不得下发新 token"

    # 队长原 token 必须仍有效，role 不变
    c, st = api.get(f"/api/teams/{tid}/state", cap_tok)
    assert c == 200, "队长原 token 应仍有效"
    assert st["me"]["role"] == "captain"
    assert len([m for m in st["members"] if m["role"] == "captain"]) == 1


def test_normal_join_still_works(srv):
    """安全修复不得影响正常新成员入队。"""
    api, _ = srv
    tid, cap_tok = _mkteam(api, "cap-B")

    c, b = api.post("/api/teams/join", {"team_id": tid, "code": CODE,
                                        "client_id": "mem-B", "nickname": "队员B"})
    assert c == 200 and b.get("token"), f"新成员应能入队，HTTP={c}"
    assert len(b.get("code_salt", "")) == 64, "必须返回 code_salt（客户端派生密钥要用）"
    assert b.get("status") == "pending"

    c, _ = api.post(f"/api/teams/{tid}/approve", {"client_id": "mem-B"}, token=cap_tok)
    assert c == 200, "队长应能批准"

    # 被踢的人不能重新 join
    c, _ = api.post(f"/api/teams/{tid}/kick", {"client_id": "mem-B"}, token=cap_tok)
    assert c == 200
    c, b = api.post("/api/teams/join", {"team_id": tid, "code": CODE,
                                        "client_id": "mem-B", "nickname": "再来"})
    assert c == 403 and b.get("error") == "kicked"


def test_ttl_validation(srv):
    """ttl 必须是合法整数且不产生「建出即过期」的队伍。"""
    api, db = srv
    c, _ = api.post("/api/teams", {"captain_client_id": "x1", "code_hash": "a" * 64,
                                   "code_salt": "b" * 64, "ttl": "abc"})
    assert c == 400, f"ttl 字符串应 400（不能是连接被掐断），HTTP={c}"

    c, _ = api.post("/api/teams", {"captain_client_id": "x2", "code_hash": "a" * 64,
                                   "code_salt": "b" * 64, "ttl": -999999})
    assert c == 200
    import sqlite3
    n = sqlite3.connect(db).execute(
        "SELECT COUNT(*) FROM teams WHERE captain_client_id='x2' "
        "AND expires_at < strftime('%s','now')").fetchone()[0]
    assert n == 0, "负 ttl 不应建出已过期的废队伍"


def test_create_rate_limit(srv):
    """建队要限流，否则可无限堆队伍撑爆数据库。"""
    api, _ = srv
    ok = 0
    limited = False
    for i in range(25):
        c, _ = api.post("/api/teams", {"captain_client_id": f"spam{i}",
                                       "code_hash": "a" * 64, "code_salt": "b" * 64,
                                       "ttl": 60})
        if c == 200:
            ok += 1
        elif c == 429:
            limited = True
            break
    assert limited, f"25 次建队应触发限流，实际成功 {ok} 次"


def test_wrong_code_rate_limit(srv):
    """错码限流不回归。"""
    api, _ = srv
    tid, _ = _mkteam(api, "cap-C")
    c, b = api.post("/api/teams/join", {"team_id": tid, "code": "000000",
                                        "client_id": "z1"})
    assert c == 403 and b.get("error") == "wrong_code"


def test_purge_expired(srv):
    """过期队伍必须能被清理（否则数据库无限增长，且 TTL 防离线枚举失效）。"""
    api, db = srv
    import sqlite3
    # 先建一个真队伍，再把它改成已过期
    tid, _ = _mkteam(api, "cap-P")
    con = sqlite3.connect(db)
    con.execute("UPDATE teams SET expires_at = strftime('%s','now') - 86400 "
                "WHERE team_id=?", (tid,))
    con.commit()
    before = con.execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    assert before >= 1

    sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "team"))
    import coordinator
    st = coordinator.Store(db)
    removed = st.purge_expired()
    st.conn.close()

    after = sqlite3.connect(db).execute("SELECT COUNT(*) FROM teams").fetchone()[0]
    assert removed > 0 and after < before, f"应删过期队伍，实际 removed={removed}"
    # 被删队伍的关联数据也要清干净，否则仍是数据泄漏/堆积
    con2 = sqlite3.connect(db)
    for tbl in ("members", "packages", "usage_log"):
        n = con2.execute(f"SELECT COUNT(*) FROM {tbl} WHERE team_id=?", (tid,)).fetchone()[0]
        assert n == 0, f"{tbl} 里仍残留已删队伍的数据: {n} 行"
    con2.close()
