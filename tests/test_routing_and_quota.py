"""A（配额账本接进路由）与 B（X-Routed-Via / X-Fallback-Detail）的集成行为。

B 的那几条盯的是一个容易被忽略的前提：响应头在第一个字节发出前就定死了。
所以「实际由谁回复」只能靠急切连接来如实回答，测试必须断言头里的名字
等于真正响应的那个候选，而不是请求里写的名字。
"""
import asyncio
import json as _json

import pytest

import app as app_module
import quota


class _SimpleResp:
    """最小可用的上游响应替身。headers 必须有 —— 账本会读它。"""

    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.text = text
        self.headers = dict(headers or {})

    def json(self):
        return _json.loads(self.text)


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


_OK = _json.dumps({
    "choices": [{"message": {"role": "assistant", "content": "ok"}}],
    "usage": {"prompt_tokens": 1, "completion_tokens": 1},
})


class _FakeHTTP:
    """替身 httpx.AsyncClient。

    app 的 http_client 在 lifespan 里才创建，直接跑函数时它是 None，
    所以要整个替换掉模块属性（跟 test_gateway_bugfix_192 同一套路）。
    """

    def __init__(self, post_fn=None, send_fn=None):
        self._post = post_fn
        self._send = send_fn

    async def post(self, url, **kw):
        return self._post(url, **kw)

    def build_request(self, method, url, **kw):
        return {"url": url, **kw}

    async def send(self, req, stream=False):
        return self._send(req["url"])


class _FakeStreamResp:
    """流式上游响应。aiter_lines 逐行吐 SSE。"""

    def __init__(self, status, lines, headers=None):
        self.status_code = status
        self._lines = list(lines)
        self.headers = dict(headers or {})
        self.text = ""

    async def aiter_lines(self):
        for line in self._lines:
            yield line

    async def aread(self):
        return b""

    async def aclose(self):
        pass


async def _consume(resp) -> str:
    """把流式响应体一次性读出来。

    body_iterator 是 async 生成器（Starlette 还包了一层线程池迭代），
    不能直接 b"".join() —— 得在事件循环里异步消费。
    """
    out = []
    async for chunk in resp.body_iterator:
        out.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    return b"".join(out).decode()


def _use_http(monkeypatch, post_fn=None, send_fn=None):
    monkeypatch.setattr(app_module, "http_client", _FakeHTTP(post_fn, send_fn))


@pytest.fixture(autouse=True)
def _clean():
    quota.reset_all()
    yield
    quota.reset_all()


def _fake_ok(url, **kw):
    return _SimpleResp(200, _OK)


# ------------------------------------------------------------------
# B：响应头
# ------------------------------------------------------------------
def test_x_routed_via_names_the_serving_provider(monkeypatch):
    _setup_two_models(monkeypatch)
    _use_http(monkeypatch, post_fn=_fake_ok)
    resp = asyncio.run(app_module.proxy_chat(_Req(
        {"model": "P/m1", "messages": [{"role": "user", "content": "hi"}]})))
    assert resp.status_code == 200
    via = resp.headers.get("X-Routed-Via")
    assert via and via.startswith("P/m1"), via
    # 上游真实模型名也带出去，方便排查别名映射
    assert "upstream: m1" in via


def test_fallback_detail_when_first_candidate_refused(monkeypatch):
    """第一个候选被拒时，头要说清跳过了谁、真正的回复来自谁。"""
    _setup_two_models(monkeypatch, names=("A", "B"), models=("m",))

    def fake(url, **kw):
        if url.startswith("http://a/"):
            return _SimpleResp(429, "slow down", {"Retry-After": "90"})
        return _SimpleResp(200, _OK)

    _use_http(monkeypatch, post_fn=fake)
    # 请求裸模型名才会跨供应商切换；点名 A/m 等于「只要 A」
    resp = asyncio.run(app_module.proxy_chat(_Req(
        {"model": "m", "messages": [{"role": "user", "content": "hi"}]})))

    assert resp.status_code == 200
    assert resp.headers.get("X-Routed-Via", "").startswith("B/m")
    detail = resp.headers.get("X-Fallback-Detail", "")
    assert "A/m" in detail and "429" in detail, detail


_SSE_OK = [
    'data: {"choices":[{"delta":{"content":"hi"},"index":0}],"model":"m"}',
    "",
    "data: [DONE]",
]


def test_stream_fallback_reports_routed_via(monkeypatch):
    """流式路径同样要急切连接：头里写的是真正连上的那个候选，不是请求里的。"""
    _setup_two_models(monkeypatch, names=("A", "B"), models=("m",))

    def send(url):
        if url.startswith("http://a/"):
            raise app_module.httpx.ConnectError("refused")
        return _FakeStreamResp(200, _SSE_OK)

    _use_http(monkeypatch, send_fn=send)
    resp = asyncio.run(app_module.proxy_chat(_Req(
        {"model": "m", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]})))

    assert resp.headers.get("X-Routed-Via", "").startswith("B/m")
    assert "A/m" in resp.headers.get("X-Fallback-Detail", "")
    body = asyncio.run(_consume(resp))
    assert "hi" in body and "[DONE]" in body


def test_all_failed_still_returns_sse_not_hang(monkeypatch):
    """一个都连不上时也要按 SSE 收尾 —— 不能让客户端挂着等超时。"""
    _setup_two_models(monkeypatch, names=("A",), models=("m",))

    def send(url):
        raise app_module.httpx.ConnectError("refused")

    _use_http(monkeypatch, send_fn=send)
    resp = asyncio.run(app_module.proxy_chat(_Req(
        {"model": "A/m", "stream": True,
         "messages": [{"role": "user", "content": "hi"}]})))
    body = asyncio.run(_consume(resp))
    assert "[DONE]" in body
    assert "所有模型均失败" in body


def test_routing_headers_are_ascii_safe():
    """中文提供方名不能直接进头 —— 非 ASCII 会让 httpx 拒发整个响应。"""
    hdr = app_module._routing_headers(
        ({"name": "乌班图Qwen3.8-27B"}, "qwen"), [])
    via = hdr["X-Routed-Via"]
    via.encode("ascii")          # 非 ASCII 会抛
    assert "Qwen3.8" in via      # 但内容仍可辨认


def test_routing_header_truncates_very_long_detail():
    tried = [f"provider-{i}/model-{i}: HTTP 429" for i in range(40)]
    hdr = app_module._routing_headers(None, tried)
    assert len(hdr["X-Fallback-Detail"]) <= 512


def test_no_routing_header_when_everything_failed():
    assert app_module._routing_headers(None, []) == {}


# ------------------------------------------------------------------
# A：额度冷却影响候选选择
# ------------------------------------------------------------------
def _setup_two_models(monkeypatch, names=("P",), models=("m1", "m2")):
    monkeypatch.setattr(app_module, "providers", [
        {"name": n, "base_url": f"http://{n[0].lower()}/v1", "api_key": "k",
         "models": list(models), "disabled_models": []}
        for n in names
    ])
    monkeypatch.setattr(app_module, "MODEL_ALIASES", {})
    for n in names:
        for m in models:
            monkeypatch.setitem(app_module.health_status, f"{n}||{m}",
                                {"status": "ok", "code": 200})
            monkeypatch.setitem(app_module.circuit_breaker, f"{n}||{m}",
                                {"fails": 0, "open_until": 0})


def test_cooled_model_sinks_below_fresh_one(monkeypatch):
    _setup_two_models(monkeypatch)
    quota.force_cooldown("P", "m1", 300, reason="daily cap")
    # 请求 m2 时不该被 m1 挡住
    assert [m for _, m in app_module.pick_available_models("P/m2")] == ["m2"]
    # 全池里 m1 直接沉到 tier 2：只有前面全空时才轮到它。
    # 这跟熔断候选同一档语义 —— 冷却不是判死，是「别排第一个上」。
    all_picked = [m for _, m in app_module.pick_available_models(None)]
    assert all_picked == ["m2"], all_picked


def test_cooling_never_removes_the_last_candidate(monkeypatch):
    """全部候选都在冷却中时仍然返回它们 —— 冷却不是判死。"""
    _setup_two_models(monkeypatch)
    quota.force_cooldown("P", "m1", 300, reason="daily cap")
    quota.force_cooldown("P", "m2", 300, reason="daily cap")
    assert len(app_module.pick_available_models(None)) == 2


def test_router_group_keeps_cooled_member_but_sinks_it(monkeypatch):
    """路由组是用户显式圈定的集合：冷却影响排序，不影响去留。"""
    _setup_two_models(monkeypatch)
    monkeypatch.setitem(app_module.ROUTERS, "组", ["m1", "m2"])
    quota.force_cooldown("P", "m1", 300, reason="daily cap")
    picked = app_module.pick_available_models("组")
    assert [m for _, m in picked] == ["m2", "m1"]


def test_probe_feeds_the_ledger(monkeypatch):
    """探测响应里的 rate-limit 头是最干净的额度证据来源。"""
    def fake(url, **kw):
        return _SimpleResp(429, "daily limit reached for requests per day",
                           {"Retry-After": "600"})
    _use_http(monkeypatch, post_fn=fake)
    asyncio.run(app_module.check_model("http://up/v1", "k", "m1", provider_name="P"))
    assert quota.is_cooling("P", "m1")
    assert quota.cooldown_left("P", "m1") >= 590


def test_quota_and_circuit_are_independent(monkeypatch):
    """配额冷却不该动熔断，熔断也不该动配额 —— 两套记账分开。"""
    _setup_two_models(monkeypatch)
    quota.force_cooldown("P", "m1", 300, reason="daily cap")
    assert app_module.circuit_breaker["P||m1"]["open_until"] == 0
    assert app_module.circuit_breaker["P||m1"]["fails"] == 0
