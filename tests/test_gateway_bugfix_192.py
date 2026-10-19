"""v1.9.2 回归：版本门禁、4xx 透传、熔断只记上游的错。

背景：v1.9.1 之前有三处会让用户「看到完全错误的提示」：
1. version.json 缺 version 字段时，旧 _version_gt 用「字符串不相等」兜底 →
   每次启动都弹「发现新版本」，版本号是空的。
2. 上游 4xx 被 continue 掉，最后统一 502「upstream 400」→ 用户永远不知道
   是哪个参数错了。
3. 4xx 也计入熔断 → 用户自己发三个坏请求，模型就被降级 60 秒。
"""
import asyncio
import json

import pytest

import app as app_module


# ============================================================
# 1. 版本比较：解析不出来就不算新版本
# ============================================================
@pytest.mark.parametrize("candidate,expected", [
    ("1.9.2", True),
    ("v1.9.2", True),
    ("V1.9.2", True),
    ("1.9.2-beta", True),      # 预发布后缀按主版本比
    ("1.9.10", True),          # 数字比较，不是字符串比较
    ("1.10.0", True),
    ("1.9.1", False),          # 相等
    ("1.9.0", False),
    ("1.9.2.0", True),         # 补零后等同 1.9.2
    ("1.9.1.0", False),        # 补零后等同 1.9.1
    ("", False),               # ← 本次修复的核心
    ("   ", False),
    (None, False),
    ("garbage", False),
    ("v", False),
])
def test_version_gt_rejects_unparsable(candidate, expected):
    assert app_module._version_gt(candidate, "1.9.1") is expected


class _FakeResp:
    def __init__(self, payload):
        self.status_code = 200
        self._payload = payload

    def json(self):
        return self._payload


class _FakeHTTP:
    def __init__(self, payload):
        self._payload = payload

    async def get(self, url, **kw):
        return _FakeResp(self._payload)


def _version_payload(version, min_version=""):
    return {
        "version": version,
        "min_version": min_version,
        "release_notes": "x",
        "download_url_windows": "https://e/w.exe",
        "download_url_linux": "https://e/l",
        "download_url_macos": "https://e/m.zip",
    }


@pytest.mark.parametrize("version", ["", None, "garbage"])
def test_broken_version_json_never_reports_update(monkeypatch, version):
    """坏掉的 version.json 不能变成「每次启动都提示新版本」。"""
    monkeypatch.setattr(app_module, "http_client", _FakeHTTP(_version_payload(version)))
    r = asyncio.run(app_module.check_update())
    assert r["has_update"] is False
    assert r["force_update"] is False


@pytest.mark.parametrize("min_version", ["", None, "garbage", "0.0.1"])
def test_force_update_ignores_unparsable_min_version(monkeypatch, min_version):
    monkeypatch.setattr(app_module, "http_client",
                        _FakeHTTP(_version_payload("9.9.9", min_version)))
    r = asyncio.run(app_module.check_update())
    assert r["has_update"] is True
    assert r["force_update"] is False


# ============================================================
# 2. 熔断门槛：4xx 是客户端的错，不是上游的错
# ============================================================
@pytest.mark.parametrize("status,is_fault", [
    (400, False), (401, False), (403, False), (404, False),
    (413, False), (422, False),
    (429, True), (500, True), (502, True), (503, True),
])
def test_provider_fault_table(status, is_fault):
    assert app_module._provider_fault(status) is is_fault


class _Upstream:
    """只回答一个固定状态码的上游。"""

    def __init__(self, status, text):
        self.status = status
        self.text = text

    async def post(self, url, **kw):
        r = _SimpleResp(self.status, self.text)
        return r


class _SimpleResp:
    """假上游响应。

    headers 必须有：非流式路径现在会读 resp.headers 喂配额账本，
    少了这个属性整条转发会 AttributeError 掉。
    """

    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.text = text
        self.headers = dict(headers or {})

    def json(self):
        return json.loads(self.text)


@pytest.fixture
def one_provider(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [{
        "name": "P", "base_url": "http://up/v1", "api_key": "k",
        "models": ["m1"], "disabled_models": [],
    }])
    monkeypatch.setattr(app_module, "MODEL_ALIASES", {})
    monkeypatch.setitem(app_module.health_status, "P||m1", {"status": "ok", "code": 200})
    monkeypatch.setitem(app_module.circuit_breaker, "P||m1", {"fails": 0, "open_until": 0})
    return "P/m1"


def _post_chat(model):
    from fastapi.testclient import TestClient
    client = TestClient(app_module.app)
    return client.post(
        "/v1/chat/completions",
        headers={"Authorization": "Bearer " + app_module.LOCAL_API_KEY},
        json={"model": model, "messages": [{"role": "user", "content": "hi"}]},
    )


def test_upstream_4xx_body_reaches_client(monkeypatch, one_provider):
    """上游 400 的原话要原样回传，不能变成 502「upstream 400」。"""
    body = json.dumps({"error": {"message": "max_tokens too large", "type": "invalid_request_error"}})
    monkeypatch.setattr(app_module, "http_client", _Upstream(400, body))
    r = _post_chat(one_provider)
    assert r.status_code == 400
    assert r.json()["error"]["message"] == "max_tokens too large"


def test_upstream_401_reaches_client(monkeypatch, one_provider):
    """key 失效时回 401，而不是「所有候选模型均失败」。"""
    monkeypatch.setattr(app_module, "http_client",
                        _Upstream(401, json.dumps({"error": {"message": "invalid api key"}})))
    r = _post_chat(one_provider)
    assert r.status_code == 401
    assert "invalid api key" in r.text


def test_upstream_500_still_502(monkeypatch, one_provider):
    """5xx 是上游的错，维持 502 —— 不能把上游故障伪装成客户端错误。"""
    monkeypatch.setattr(app_module, "http_client", _Upstream(503, "boom"))
    r = _post_chat(one_provider)
    assert r.status_code == 502


def test_4xx_does_not_trip_circuit(monkeypatch, one_provider):
    monkeypatch.setattr(app_module, "http_client",
                        _Upstream(400, json.dumps({"error": {"message": "bad"}})))
    monkeypatch.setitem(app_module.circuit_breaker, "P||m1", {"fails": 0, "open_until": 0})
    _post_chat(one_provider)
    assert app_module.circuit_breaker["P||m1"]["fails"] == 0


def test_5xx_does_trip_circuit(monkeypatch, one_provider):
    monkeypatch.setattr(app_module, "http_client", _Upstream(500, "boom"))
    monkeypatch.setitem(app_module.circuit_breaker, "P||m1", {"fails": 0, "open_until": 0})
    _post_chat(one_provider)
    assert app_module.circuit_breaker["P||m1"]["fails"] == 1
