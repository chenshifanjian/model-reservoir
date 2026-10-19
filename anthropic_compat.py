"""Anthropic Messages API（`POST /v1/messages`）⇄ OpenAI Chat Completions 翻译。

存在的唯一理由：Claude Code、Cline 这类客户端只认 Anthropic 线格式，
它们没法指向一个 OpenAI 兼容网关。这个模块让它们能用上我们聚合的免费档。

三条约束，都是照着官方 Messages API reference 定的：

1. **不做能力，只做翻译。** 本模块不碰网络、不选模型、不记账 —— 
   翻译成 OpenAI 请求后交给 app.py 那套既有的路由/熔断/配额/前缀注入，
   保证 `/v1/messages` 与 `/v1/chat/completions` 走的是同一条路。
   两套转发逻辑分头演进是这类网关最常见的腐烂方式。
2. **未知字段丢弃而不是报错。** Anthropic 那边新增的字段（extended thinking、
   service_tier、citations）我们没实现，静默丢弃比 400 更适合网关：
   客户端拿到一个能用的回复，好过拿到一个「不支持 xxx」。
3. **tool_result 必须先于其他 user 内容。** Anthropic 的规矩是 tool_result 
   块必须出现在 user 消息开头；OpenAI 更严格 —— tool 消息必须紧跟在发起
   tool_call 的 assistant 消息后面。翻译时按 OpenAI 的顺序约束重排。
"""

from __future__ import annotations

import json
import uuid
from typing import AsyncIterator

# Anthropic stop_reason ⇄ OpenAI finish_reason。
# 中间值是「两边都没有直接对应」时的兜底，宁可保守也别瞎映射。
_STOP_TO_FINISH = {
    "end_turn": "stop",
    "max_tokens": "length",
    "stop_sequence": "stop",
    "tool_use": "tool_calls",
    "pause_turn": "stop",
    "refusal": "content_filter",
}
_FINISH_TO_STOP = {
    "stop": "end_turn",
    "length": "max_tokens",
    "tool_calls": "tool_use",
    "content_filter": "refusal",
    "function_call": "tool_use",
}


# ============================================================
# 请求方向：Anthropic → OpenAI
# ============================================================
def flatten_text(content) -> str:
    """把 string 或 text 块数组拍平成一段文本。"""
    if content is None:
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(b.get("text", "") for b in content
                       if isinstance(b, dict) and b.get("type") == "text")
    return str(content)


def _image_url(block: dict) -> str | None:
    src = block.get("source") or {}
    if src.get("type") == "base64":
        media = src.get("media_type") or "image/png"
        data = src.get("data") or ""
        return f"data:{media};base64,{data}"
    if src.get("type") == "url":
        return src.get("url")
    # 官方还有一种 {"type":"file"}，网关不支持就当没看见
    return None


def _tool_to_openai(t: dict) -> dict:
    return {
        "type": "function",
        "function": {
            "name": t.get("name") or "",
            "description": t.get("description") or "",
            # Anthropic 的入参 schema 叫 input_schema，OpenAI 叫 parameters，
            # 结构本身都是 JSON Schema，原样搬
            "parameters": t.get("input_schema") or {"type": "object"},
        },
    }


def _tool_choice_to_openai(tc):
    if not isinstance(tc, dict):
        return None
    kind = tc.get("type")
    if kind == "auto":
        return "auto"
    if kind == "any":                      # 「必须调一个工具」
        return "required"
    if kind == "none":
        return "none"
    if kind == "tool":
        return {"type": "function", "function": {"name": tc.get("name") or ""}}
    return None


def _convert_message(role: str, content) -> list[dict]:
    """一条 Anthropic 消息 → 一到多条 OpenAI 消息。

    会拆分的原因：Anthropic 把 tool_use / tool_result 塞在同一层 content 里，
    而 OpenAI 要求它们各自成条，且 tool 消息必须紧跟发起方。
    """
    if content is None:
        return []
    if isinstance(content, str):
        return [{"role": role, "content": content}]

    if not isinstance(content, list):
        return [{"role": role, "content": str(content)}]

    text_parts: list[str] = []
    images: list[str] = []
    tool_calls: list[dict] = []
    tool_results: list[dict] = []

    for b in content:
        if not isinstance(b, dict):
            continue
        bt = b.get("type")
        if bt == "text":
            text_parts.append(b.get("text") or "")
        elif bt == "image":
            url = _image_url(b)
            if url:
                images.append(url)
        elif bt == "tool_use":
            args = b.get("input")
            # OpenAI 的 arguments 是 JSON 字符串，不是对象
            if isinstance(args, str):
                raw = args
            else:
                raw = json.dumps(args if args is not None else {},
                                 ensure_ascii=False)
            tool_calls.append({
                "id": b.get("id") or f"call_{uuid.uuid4().hex[:24]}",
                "type": "function",
                "function": {"name": b.get("name") or "", "arguments": raw},
            })
        elif bt == "tool_result":
            tool_results.append(_tool_result_to_openai(b))
        # thinking / redacted_thinking：网关不支持扩展思考，丢弃

    out: list[dict] = []

    # ① tool 消息必须排在最前，且要落在发起 tool_call 的 assistant 之后。
    #    一条 user 消息里带多个 tool_result → 拆成多条 tool 消息。
    out.extend(tool_results)

    text = "".join(text_parts)
    if role == "assistant":
        msg: dict = {"role": "assistant", "content": text or None}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        # OpenAI 不接受 content 和 tool_calls 同时为空
        if msg["content"] is None and not tool_calls:
            return []
        out.append(msg)
    else:
        if images:
            parts: list[dict] = []
            if text:
                parts.append({"type": "text", "text": text})
            parts.extend({"type": "image_url", "image_url": {"url": u}}
                         for u in images)
            out.append({"role": "user", "content": parts})
        elif text:
            out.append({"role": "user", "content": text})
    return out


def _tool_result_to_openai(b: dict) -> dict:
    content = b.get("content")
    if isinstance(content, list):
        # 只取 text 块；tool_result 里的图片罕见，丢了比拼错顺序安全
        text = flatten_text(content)
    else:
        text = content if content is not None else ""
    if b.get("is_error"):
        text = f"[tool error] {text}"
    return {
        "role": "tool",
        "tool_call_id": b.get("tool_use_id") or "",
        "content": text if isinstance(text, str) else json.dumps(text,
                                                                  ensure_ascii=False),
    }


def to_openai_request(body: dict) -> dict:
    """Anthropic 请求体 → OpenAI 请求体。

    model 原样透传：我们的路由认裸模型名与 `供应商/模型名`，
    Anthropic 对 model 字段不做格式校验，客户端传什么都能路由。
    """
    body = body or {}
    out: dict = {"model": body.get("model") or ""}

    # max_tokens 在 Anthropic 是必填，但网关不为难客户端：缺了就不发
    if body.get("max_tokens") is not None:
        out["max_tokens"] = body["max_tokens"]

    messages: list[dict] = []
    system = body.get("system")
    if system:
        messages.append({"role": "system", "content": flatten_text(system)})
    for m in body.get("messages") or []:
        if not isinstance(m, dict):
            continue
        messages.extend(_convert_message(m.get("role") or "user",
                                         m.get("content")))
    out["messages"] = messages

    for src, dst in (("temperature", "temperature"), ("top_p", "top_p"),
                     ("top_k", "top_k")):
        if body.get(src) is not None:
            out[dst] = body[src]

    if body.get("stop_sequences"):
        out["stop"] = list(body["stop_sequences"])

    if body.get("stream"):
        out["stream"] = True

    tools = body.get("tools")
    if tools:
        out["tools"] = [_tool_to_openai(t) for t in tools if isinstance(t, dict)]
    tc = _tool_choice_to_openai(body.get("tool_choice"))
    if tc is not None:
        out["tool_choice"] = tc

    # 丢弃不支持的字段：metadata / thinking / service_tier / citations
    return out


# ============================================================
# 响应方向：OpenAI → Anthropic
# ============================================================
def _stop_reason_for(finish_reason: str | None, content: list[dict]) -> str:
    """由 finish_reason 推 Anthropic stop_reason。

    content 优先：有些上游在 tool_calls 时给的 finish_reason 是 "stop"，
    那会让客户端以为对话结束了、不再把 tool_result 回传。
    """
    if any(b.get("type") == "tool_use" for b in content):
        return "tool_use"
    if finish_reason == "length":
        return "max_tokens"
    if finish_reason == "content_filter":
        return "refusal"
    return _FINISH_TO_STOP.get(finish_reason or "", "end_turn")


def new_message_id() -> str:
    return "msg_" + uuid.uuid4().hex[:24]


def from_openai_response(oa: dict, requested_model: str,
                         message_id: str | None = None) -> dict:
    """OpenAI chat completion → Anthropic message。"""
    oa = oa or {}
    choices = oa.get("choices") or [{}]
    choice = choices[0] if choices else {}
    msg = choice.get("message") or {}

    content: list[dict] = []
    text = msg.get("content")
    if isinstance(text, str) and text:
        content.append({"type": "text", "text": text})

    reasoning = msg.get("reasoning_content") or msg.get("reasoning")
    if isinstance(reasoning, str) and reasoning:
        # signature 留空：OpenAI 侧没有这个概念，客户端不该拿它做校验
        content.append({"type": "thinking", "thinking": reasoning,
                        "signature": ""})

    for tc in msg.get("tool_calls") or []:
        if not isinstance(tc, dict):
            continue
        fn = tc.get("function") or {}
        args = fn.get("arguments")
        if isinstance(args, str):
            try:
                parsed = json.loads(args) if args.strip() else {}
            except json.JSONDecodeError:
                # 参数不是合法 JSON：原样带出去，好过丢掉让客户端重试
                parsed = {"_raw": args}
        elif isinstance(args, dict):
            parsed = args
        else:
            parsed = {}
        content.append({
            "type": "tool_use",
            "id": tc.get("id") or f"toolu_{uuid.uuid4().hex[:24]}",
            "name": fn.get("name") or "",
            "input": parsed,
        })

    usage = oa.get("usage") or {}
    return {
        # 一律用 msg_ 前缀：Anthropic 的 id 形如 msg_012...，
        # 透传上游的 chatcmpl-1 既不符合格式，也把上游身份泄露给了客户端
        "id": message_id or new_message_id(),
        "type": "message",
        "role": "assistant",
        "model": requested_model,
        "content": content,
        "stop_reason": _stop_reason_for(choice.get("finish_reason"), content),
        "stop_sequence": None,
        "usage": {
            "input_tokens": usage.get("prompt_tokens", 0) or 0,
            "output_tokens": usage.get("completion_tokens", 0) or 0,
            "cache_creation_input_tokens":
                usage.get("cache_creation_input_tokens", 0) or 0,
            "cache_read_input_tokens":
                usage.get("cache_read_input_tokens", 0) or 0,
        },
    }


def error_body(err_type: str, message: str) -> dict:
    """Anthropic 错误体。客户端靠 type 决定要不要重试。"""
    return {"type": "error", "error": {"type": err_type, "message": message}}


def status_to_error_type(status: int) -> str:
    return {
        400: "invalid_request_error",
        401: "authentication_error",
        403: "permission_not_found_error",
        404: "not_found_error",
        413: "request_too_large",
        429: "rate_limit_error",
        500: "api_error",
        503: "overloaded_error",   # 我们用 503 表达「没有可用模型」
        529: "overloaded_error",
    }.get(status, "api_error")


# ============================================================
# 流式：OpenAI SSE → Anthropic SSE
# ============================================================
def _event(name: str, data: dict) -> str:
    return f"event: {name}\ndata: {json.dumps(data, ensure_ascii=False)}\n\n"


async def sse_to_anthropic(chunks: AsyncIterator[str],
                           requested_model: str,
                           message_id: str | None = None) -> AsyncIterator[str]:
    """把 OpenAI 的 SSE 流翻译成 Anthropic 的事件流。

    事件顺序是硬约束：message_start →（content_block_start → 
    content_block_delta* → content_block_stop）×N → message_delta → message_stop。
    客户端 SDK 按这个顺序建状态机，少一个 stop 事件就会挂住不返回。
    """
    mid = message_id or new_message_id()
    input_tokens = 0
    output_tokens = 0
    stop_reason = None

    # 当前正在写入的块：text 用 index，tool_use 用 index + 累积的 JSON 片段
    open_block: dict | None = None     # {"index", "type", "name", "id", "json"}
    text_started = False
    # OpenAI 的 tool_calls 按 index 分片到达，这里按 index 收集
    tool_blocks: dict[int, dict] = {}

    yield _event("message_start", {
        "type": "message_start",
        "message": {
            "id": mid, "type": "message", "role": "assistant",
            "model": requested_model, "content": [],
            "stop_reason": None, "stop_sequence": None,
            "usage": {"input_tokens": 0, "output_tokens": 0},
        },
    })
    # 官方实现会在长回复中间发 ping，客户端 SDK 期待它出现（但不强制）
    yield _event("ping", {"type": "ping"})

    async def close_block():
        nonlocal open_block
        if open_block is not None:
            yield _event("content_block_stop", {
                "type": "content_block_stop",
                "index": open_block["index"],
            })
            open_block = None

    index = -1

    async for raw in chunks:
        if not raw:
            continue
        line = raw.strip()
        if not line.startswith("data:"):
            continue                       # 注释行 / 空行 / event: 行，跳过
        payload = line[5:].strip()
        if payload == "[DONE]":
            break
        try:
            obj = json.loads(payload)
        except json.JSONDecodeError:
            continue

        if obj.get("usage"):
            u = obj["usage"]
            input_tokens = u.get("prompt_tokens", input_tokens) or input_tokens
            output_tokens = u.get("completion_tokens", output_tokens) or output_tokens

        choices = obj.get("choices") or []
        if not choices:
            continue
        choice = choices[0]
        delta = choice.get("delta") or {}

        # ① 文本
        text = delta.get("content")
        if isinstance(text, str) and text:
            if open_block is None or open_block["type"] != "text":
                async for c in close_block():
                    yield c
                index += 1
                open_block = {"index": index, "type": "text"}
                yield _event("content_block_start", {
                    "type": "content_block_start",
                    "index": index,
                    "content_block": {"type": "text", "text": ""},
                })
                text_started = True
            yield _event("content_block_delta", {
                "type": "content_block_delta",
                "index": open_block["index"],
                "delta": {"type": "text_delta", "text": text},
            })

        # ② 工具调用：OpenAI 按 index 分片，arguments 是 JSON 片段
        for tc in delta.get("tool_calls") or []:
            if not isinstance(tc, dict):
                continue
            i = tc.get("index", 0) or 0
            blk = tool_blocks.get(i)
            if blk is None:
                blk = {"index": None, "id": None, "name": "", "json": ""}
                tool_blocks[i] = blk
            if tc.get("id"):
                blk["id"] = tc["id"]
            fn = tc.get("function") or {}
            if fn.get("name"):
                blk["name"] = (blk["name"] or "") + fn["name"]
            if fn.get("arguments"):
                blk["json"] += fn["arguments"]

        # ③ 收尾原因
        fr = choice.get("finish_reason")
        if fr:
            stop_reason = _FINISH_TO_STOP.get(fr, "end_turn")

    # 把收集到的 tool_calls 逐个吐成独立的 content block
    # （必须在 message_delta 之前 —— 客户端据此决定要不要回传 tool_result）
    has_tool = False
    for i in sorted(tool_blocks):
        blk = tool_blocks[i]
        if not blk["name"] and not blk["json"]:
            continue
        has_tool = True
        async for c in close_block():
            yield c
        index += 1
        yield _event("content_block_start", {
            "type": "content_block_start",
            "index": index,
            "content_block": {
                "type": "tool_use",
                "id": blk["id"] or f"toolu_{uuid.uuid4().hex[:24]}",
                "name": blk["name"],
                "input": {},
            },
        })
        open_block = {"index": index, "type": "tool_use"}
        if blk["json"]:
            yield _event("content_block_delta", {
                "type": "content_block_delta",
                "index": index,
                "delta": {"type": "input_json_delta", "partial_json": blk["json"]},
            })

    async for c in close_block():
        yield c

    if has_tool and stop_reason is None:
        stop_reason = "tool_use"
    if stop_reason is None:
        stop_reason = "end_turn" if text_started else "max_tokens"

    yield _event("message_delta", {
        "type": "message_delta",
        "delta": {"stop_reason": stop_reason, "stop_sequence": None},
        "usage": {"input_tokens": input_tokens, "output_tokens": output_tokens},
    })
    yield _event("message_stop", {"type": "message_stop"})
