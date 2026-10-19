"""Anthropic Messages 面（D）：翻译正确性 + 端到端。

翻译层的断言写得比别的模块死 —— 线格式是跟别人的客户端签的合同，
改一个字段名不会在我们这边报错，只会在 Claude Code 里表现为
「调用发出去了但工具没被执行」这种极难查的故障。
"""
import asyncio
import json as _json

import pytest

import anthropic_compat as ac
import app as app_module


class _SimpleResp:
    def __init__(self, status, text, headers=None):
        self.status_code = status
        self.text = text
        self.headers = dict(headers or {})

    def json(self):
        return _json.loads(self.text)


class _FakeStreamResp:
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


class _FakeHTTP:
    def __init__(self, post_fn=None, send_fn=None):
        self._post = post_fn
        self._send = send_fn

    async def post(self, url, **kw):
        return self._post(url, **kw)

    def build_request(self, method, url, **kw):
        return {"url": url, **kw}

    async def send(self, req, stream=False):
        return self._send(req["url"])


class _Req:
    def __init__(self, body):
        self._body = body

    async def json(self):
        return self._body


_OK = _json.dumps({
    "id": "chatcmpl-1", "model": "m1",
    "choices": [{"index": 0, "finish_reason": "stop",
                 "message": {"role": "assistant", "content": "你好"}}],
    "usage": {"prompt_tokens": 7, "completion_tokens": 3, "total_tokens": 10},
})


@pytest.fixture(autouse=True)
def _setup(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [{
        "name": "P", "base_url": "http://up/v1", "api_key": "k",
        "models": ["m1"], "disabled_models": [],
    }])
    monkeypatch.setattr(app_module, "MODEL_ALIASES", {})
    monkeypatch.setitem(app_module.health_status, "P||m1",
                        {"status": "ok", "code": 200})
    monkeypatch.setitem(app_module.circuit_breaker, "P||m1",
                        {"fails": 0, "open_until": 0})


def _use_http(monkeypatch, post_fn=None, send_fn=None):
    monkeypatch.setattr(app_module, "http_client", _FakeHTTP(post_fn, send_fn))


def _call(body):
    return asyncio.run(app_module.anthropic_messages(_Req(body)))


async def _consume(resp) -> str:
    out = []
    async for chunk in resp.body_iterator:
        out.append(chunk if isinstance(chunk, bytes) else str(chunk).encode())
    return b"".join(out).decode()


# ============================================================
# 请求方向翻译
# ============================================================
def test_request_system_becomes_system_message():
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 100, "system": "你是助手",
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert oa["messages"][0] == {"role": "system", "content": "你是助手"}
    assert oa["messages"][1] == {"role": "user", "content": "hi"}
    assert oa["max_tokens"] == 100


def test_request_system_as_block_array():
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 10,
        "system": [{"type": "text", "text": "A"}, {"type": "text", "text": "B"}],
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert oa["messages"][0] == {"role": "system", "content": "AB"}


def test_request_image_becomes_data_url():
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 10,
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image", "source": {"type": "base64",
                                         "media_type": "image/png",
                                         "data": "AAAA"}},
        ]}],
    })
    parts = oa["messages"][0]["content"]
    assert parts[0] == {"type": "text", "text": "看图"}
    assert parts[1]["image_url"]["url"] == "data:image/png;base64,AAAA"


def test_request_tool_use_and_result_ordering():
    """tool 消息必须紧跟发起方 —— OpenAI 侧的硬约束。"""
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 10,
        "messages": [
            {"role": "user", "content": "查天气"},
            {"role": "assistant", "content": [
                {"type": "text", "text": "我查一下"},
                {"type": "tool_use", "id": "toolu_1", "name": "get_weather",
                 "input": {"city": "北京"}},
            ]},
            {"role": "user", "content": [
                {"type": "tool_result", "tool_use_id": "toolu_1",
                 "content": "晴 25 度"},
                {"type": "text", "text": "然后呢"},
            ]},
        ],
    })
    roles = [m["role"] for m in oa["messages"]]
    assert roles == ["user", "assistant", "tool", "user"]
    tool_msg = oa["messages"][2]
    assert tool_msg["tool_call_id"] == "toolu_1"
    assert tool_msg["content"] == "晴 25 度"
    assert oa["messages"][1]["tool_calls"][0]["function"]["arguments"] == \
        '{"city": "北京"}'
    # 文本跟在 tool 消息后面
    assert oa["messages"][3]["content"] == "然后呢"


def test_request_tool_use_arguments_are_json_string():
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 10,
        "messages": [{"role": "assistant", "content": [
            {"type": "tool_use", "id": "t1", "name": "f", "input": {"a": 1}},
        ]}],
    })
    args = oa["messages"][0]["tool_calls"][0]["function"]["arguments"]
    assert isinstance(args, str)
    assert _json.loads(args) == {"a": 1}


def test_request_tools_and_tool_choice_mapping():
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "tools": [{"name": "f", "description": "d",
                   "input_schema": {"type": "object"}}],
    })
    assert oa["tools"] == [{"type": "function", "function": {
        "name": "f", "description": "d",
        "parameters": {"type": "object"}}}]

    for anthropic, openai in (
        ({"type": "auto"}, "auto"),
        ({"type": "any"}, "required"),
        ({"type": "tool", "name": "f"},
         {"type": "function", "function": {"name": "f"}}),
    ):
        got = ac.to_openai_request({
            "model": "m1", "max_tokens": 10,
            "messages": [{"role": "user", "content": "hi"}],
            "tool_choice": anthropic,
        })["tool_choice"]
        assert got == openai, (anthropic, got)


def test_request_drops_unsupported_fields():
    """未知字段静默丢弃，不要 400 —— 网关宁可少个能力也别拒服务。"""
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 10,
        "messages": [{"role": "user", "content": "hi"}],
        "metadata": {"user_id": "u1"},
        "thinking": {"type": "enabled", "budget_tokens": 1024},
        "service_tier": "auto",
    })
    for k in ("metadata", "thinking", "service_tier"):
        assert k not in oa
    assert "top_k" not in oa


def test_request_stop_sequences_and_sampling():
    oa = ac.to_openai_request({
        "model": "m1", "max_tokens": 10, "temperature": 0.4, "top_p": 0.9,
        "top_k": 40, "stop_sequences": ["END"],
        "messages": [{"role": "user", "content": "hi"}],
    })
    assert oa["stop"] == ["END"]
    assert oa["temperature"] == 0.4 and oa["top_p"] == 0.9 and oa["top_k"] == 40


# ============================================================
# 响应方向翻译
# ============================================================
def test_response_text_block_and_usage():
    msg = ac.from_openai_response(_json.loads(_OK), "m1")
    assert msg["type"] == "message" and msg["role"] == "assistant"
    assert msg["id"].startswith("msg_")
    assert msg["content"] == [{"type": "text", "text": "你好"}]
    assert msg["stop_reason"] == "end_turn"
    assert msg["usage"] == {"input_tokens": 7, "output_tokens": 3,
                            "cache_creation_input_tokens": 0,
                            "cache_read_input_tokens": 0}


def test_response_tool_calls_become_tool_use_blocks():
    oa = _json.loads(_OK)
    oa["choices"][0]["finish_reason"] = "tool_calls"
    oa["choices"][0]["message"] = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "call_1", "type": "function",
                        "function": {"name": "f", "arguments": '{"a":1}'}}],
    }
    msg = ac.from_openai_response(oa, "m1")
    assert msg["content"] == [{"type": "tool_use", "id": "call_1",
                               "name": "f", "input": {"a": 1}}]
    assert msg["stop_reason"] == "tool_use"


def test_response_tool_use_wins_over_stop_finish_reason():
    """有些上游在 tool_calls 时给 finish_reason=stop —— 不能因此说对话结束了，
    否则客户端不会回传 tool_result。"""
    oa = _json.loads(_OK)
    oa["choices"][0]["finish_reason"] = "stop"
    oa["choices"][0]["message"]["tool_calls"] = [
        {"id": "call_1", "type": "function",
         "function": {"name": "f", "arguments": "{}"}}]
    assert ac.from_openai_response(oa, "m1")["stop_reason"] == "tool_use"


@pytest.mark.parametrize("finish,expect", [
    ("stop", "end_turn"), ("length", "max_tokens"),
    ("content_filter", "refusal"), (None, "end_turn"),
])
def test_response_stop_reason_table(finish, expect):
    oa = _json.loads(_OK)
    oa["choices"][0]["finish_reason"] = finish
    assert ac.from_openai_response(oa, "m1")["stop_reason"] == expect


def test_response_bad_tool_arguments_not_dropped():
    oa = _json.loads(_OK)
    oa["choices"][0]["message"] = {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": "c", "type": "function",
                        "function": {"name": "f", "arguments": "{not json"}}],
    }
    blk = ac.from_openai_response(oa, "m1")["content"][0]
    assert blk["type"] == "tool_use"
    assert blk["input"]["_raw"] == "{not json"


def test_error_body_shape():
    b = ac.error_body("rate_limit_error", "slow down")
    assert b == {"type": "error",
                 "error": {"type": "rate_limit_error", "message": "slow down"}}
    assert ac.status_to_error_type(429) == "rate_limit_error"
    assert ac.status_to_error_type(503) == "overloaded_error"


# ============================================================
# 流式翻译
# ============================================================
def _events(raw: str) -> list[tuple[str, dict]]:
    out = []
    for chunk in raw.split("\n\n"):
        if not chunk.strip():
            continue
        name = data = None
        for line in chunk.splitlines():
            if line.startswith("event: "):
                name = line[7:].strip()
            elif line.startswith("data: "):
                data = _json.loads(line[6:])
        if name:
            out.append((name, data))
    return out


async def _fake_openai_sse(lines):
    for line in lines:
        yield line


SSE_TEXT = [
    'data: {"choices":[{"delta":{"content":"你"},"index":0}]}',
    "",
    'data: {"choices":[{"delta":{"content":"好"},"index":0}],"usage":{"prompt_tokens":5,"completion_tokens":2}}',
    "",
    'data: {"choices":[{"delta":{},"finish_reason":"stop","index":0}]}',
    "",
    "data: [DONE]",
]


def test_stream_event_order_for_text():
    raw = asyncio.run(_consume_stream(SSE_TEXT))
    names = [n for n, _ in _events(raw)]
    assert names == ["message_start", "ping", "content_block_start",
                     "content_block_delta", "content_block_delta",
                     "content_block_stop", "message_delta", "message_stop"]
    ev = dict(_events(raw))
    assert ev["message_start"]["message"]["usage"] == {
        "input_tokens": 0, "output_tokens": 0}
    assert ev["message_delta"]["delta"]["stop_reason"] == "end_turn"
    assert ev["message_delta"]["usage"] == {
        "input_tokens": 5, "output_tokens": 2}


def test_stream_text_deltas_concatenate():
    raw = asyncio.run(_consume_stream(SSE_TEXT))
    text = "".join(d["delta"]["text"] for n, d in _events(raw)
                   if n == "content_block_delta" and d["delta"]["type"] == "text_delta")
    assert text == "你好"


SSE_TOOL = [
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"id":"call_1","function":{"name":"get_","arguments":""}}]},"index":0}]}',
    "",
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"name":"weather","arguments":"{\\"city\\":"}}]},"index":0}]}',
    "",
    'data: {"choices":[{"delta":{"tool_calls":[{"index":0,"function":{"arguments":"\\"北京\\"}"}}]},"index":0}]}',
    "",
    'data: {"choices":[{"delta":{},"finish_reason":"tool_calls","index":0}]}',
    "",
    "data: [DONE]",
]


def test_stream_tool_use_accumulates_json_fragments():
    """arguments 是分片到达的原始 JSON 片段，必须原样拼起来再给客户端。"""
    raw = asyncio.run(_consume_stream(SSE_TOOL))
    evs = _events(raw)
    starts = [d for n, d in evs if n == "content_block_start"]
    assert starts[-1]["content_block"]["type"] == "tool_use"
    assert starts[-1]["content_block"]["input"] == {}      # 官方就是空对象
    frag = "".join(d["delta"]["partial_json"] for n, d in evs
                   if n == "content_block_delta"
                   and d["delta"]["type"] == "input_json_delta")
    assert _json.loads(frag) == {"city": "北京"}
    # 收尾顺序：block_stop 在 message_delta 之前
    names = [n for n, _ in evs]
    assert names.index("content_block_stop") < names.index("message_delta")
    assert names[-1] == "message_stop"
    md = [d for n, d in evs if n == "message_delta"][0]
    assert md["delta"]["stop_reason"] == "tool_use"


async def _consume_stream(lines) -> str:
    out = []
    async for chunk in ac.sse_to_anthropic(_fake_openai_sse(lines), "m1"):
        out.append(chunk)
    return "".join(out)


# ============================================================
# 端到端
# ============================================================
def test_endpoint_requires_model(monkeypatch):
    _use_http(monkeypatch, post_fn=lambda url, **kw: _SimpleResp(200, _OK))
    resp = _call({"max_tokens": 10,
                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400
    body = _json.loads(resp.body)
    assert body["type"] == "error"
    assert body["error"]["type"] == "invalid_request_error"


def test_endpoint_503_when_no_candidate(monkeypatch):
    monkeypatch.setattr(app_module, "providers", [])
    _use_http(monkeypatch, post_fn=lambda url, **kw: _SimpleResp(200, _OK))
    resp = _call({"model": "不存在的模型", "max_tokens": 10,
                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 503
    assert _json.loads(resp.body)["error"]["type"] == "overloaded_error"


def test_endpoint_non_stream_end_to_end(monkeypatch):
    _use_http(monkeypatch, post_fn=lambda url, **kw: _SimpleResp(200, _OK))
    resp = _call({"model": "P/m1", "max_tokens": 100,
                  "system": "你是助手",
                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 200
    msg = _json.loads(resp.body)
    assert msg["type"] == "message"
    assert msg["model"] == "P/m1"                  # 回显客户端传来的名字
    assert msg["content"][0]["text"].endswith("你好")
    assert msg["usage"]["input_tokens"] == 7
    assert resp.headers.get("X-Routed-Via", "").startswith("P/m1")


def test_endpoint_passes_through_upstream_4xx(monkeypatch):
    err = _json.dumps({"error": {"message": "bad request: max_tokens too large",
                                 "type": "invalid_request_error"}})
    _use_http(monkeypatch, post_fn=lambda url, **kw: _SimpleResp(400, err))
    resp = _call({"model": "P/m1", "max_tokens": 10**9,
                  "messages": [{"role": "user", "content": "hi"}]})
    assert resp.status_code == 400
    body = _json.loads(resp.body)
    assert body["error"]["type"] == "invalid_request_error"
    assert "max_tokens" in body["error"]["message"]


def test_endpoint_stream_end_to_end(monkeypatch):
    _use_http(monkeypatch, send_fn=lambda url: _FakeStreamResp(200, SSE_TEXT))
    resp = _call({"model": "P/m1", "max_tokens": 100, "stream": True,
                  "messages": [{"role": "user", "content": "hi"}]})
    raw = asyncio.run(_consume(resp))
    names = [n for n, _ in _events(raw)]
    assert names[0] == "message_start" and names[-1] == "message_stop"
    text = "".join(d["delta"]["text"] for n, d in _events(raw)
                   if n == "content_block_delta")
    # 「🤖 模型名」前缀与 OpenAI 路径保持一致：两条路是同一个网关，
    # 客户端不该因为换了协议就看不到实际由谁回复
    assert text.startswith("🤖 P/m1")
    assert text.endswith("你好")
    assert resp.headers.get("X-Routed-Via", "").startswith("P/m1")


def test_endpoint_bad_json_body(monkeypatch):
    class _Bad:
        async def json(self):
            raise ValueError("nope")
    resp = asyncio.run(app_module.anthropic_messages(_Bad()))
    assert resp.status_code == 400
