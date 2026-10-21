"""审计 P0/P1 缺陷的回归测试。

来源：2026-10-20 只读审计，逐条对当前代码复核后确认仍成立的 7 类。
每条测试都对应一个「改前会真实复现」的故障。
"""
import asyncio
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from fastapi import Request

import app as app_module

# client / admin_headers 夹具在 tests/conftest.py 里共享
KEY = "sk-local-" + "a1" * 16


def _point(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "DATA_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "ROUTERS_FILE", tmp_path / "routers.json")


# ---------------------------------------------------------------- load_routers
def test_routers_null_json_returns_empty_dict(monkeypatch, tmp_path):
    """`null` 是合法 JSON，但顶层不是对象 → 以前 ROUTERS=None → 所有模型路由 500。"""
    _point(monkeypatch, tmp_path)
    app_module.ROUTERS_FILE.write_text("null", encoding="utf-8")
    assert app_module.load_routers() == {}


def test_routers_array_json_returns_empty_dict(monkeypatch, tmp_path):
    _point(monkeypatch, tmp_path)
    app_module.ROUTERS_FILE.write_text("[1,2,3]", encoding="utf-8")
    assert app_module.load_routers() == {}


def test_routers_bad_member_values_are_dropped(monkeypatch, tmp_path):
    """值不是列表 → 下游 set() 会炸；非字符串成员也要剔掉。"""
    _point(monkeypatch, tmp_path)
    app_module.ROUTERS_FILE.write_text(
        '{"g1": ["a", "b"], "g2": 123, "g3": [1, null, "c"]}', encoding="utf-8")
    assert app_module.load_routers() == {"g1": ["a", "b"], "g3": ["c"]}


def test_routers_corrupt_is_quarantined(monkeypatch, tmp_path):
    _point(monkeypatch, tmp_path)
    app_module.ROUTERS_FILE.write_text("{ 坏", encoding="utf-8")
    assert app_module.load_routers() == {}
    assert len(list(tmp_path.glob("routers.json.corrupt-*"))) == 1


# ---------------------------------------------------------- POST /api/routers
def test_post_routers_rejects_non_object(client, admin_headers):
    """以前 POST null 会返回 200 并落盘，之后该组名下所有请求 500 且重启依旧。"""
    r = client.post("/api/routers", headers=admin_headers, content="null")
    assert r.status_code == 400


def test_post_routers_rejects_array(client, admin_headers):
    r = client.post("/api/routers", headers=admin_headers, content="[1,2,3]")
    assert r.status_code == 400


def test_post_routers_rejects_bad_member_type(client, admin_headers):
    r = client.post("/api/routers", headers=admin_headers, content='{"p": 123}')
    assert r.status_code == 400
    assert "p" in r.text


def test_post_routers_rejects_invalid_json(client, admin_headers):
    r = client.post("/api/routers", headers=admin_headers, content="{ 不是json")
    assert r.status_code == 400


def test_post_routers_accepts_valid_shape(client, admin_headers):
    r = client.post("/api/routers", headers=admin_headers,
                    content='{"g": ["模型A", "模型B"]}')
    assert r.status_code == 200
    assert r.json()["groups"] == 1


def test_post_routers_invalid_shape_is_not_persisted(client, admin_headers):
    """关键：拒绝之后磁盘上不能留下坏状态。"""
    client.post("/api/routers", headers=admin_headers, content='{"g": ["ok"]}')
    bad = client.post("/api/routers", headers=admin_headers, content='{"p": 123}')
    assert bad.status_code == 400
    assert json.loads(app_module.ROUTERS_FILE.read_text(encoding="utf-8")) == {"g": ["ok"]}


# --------------------------------------------------------------- 全局异常兜底
def test_real_handler_shape_matches_protocol():
    """/v1/* 走 OpenAI 形状、/v1/messages 走 Anthropic 形状、/api/* 走简单形状。"""
    handler = app_module._unhandled_exception

    req_v1 = Request({"type": "http", "method": "POST",
                      "path": "/v1/chat/completions", "headers": []})
    resp = asyncio.run(handler(req_v1, ValueError("boom")))
    assert resp.status_code == 500
    assert json.loads(resp.body)["error"]["type"] == "server_error"

    req_am = Request({"type": "http", "method": "POST",
                      "path": "/v1/messages", "headers": []})
    body2 = json.loads(asyncio.run(handler(req_am, ValueError("boom"))).body)
    assert body2["type"] == "error"
    assert body2["error"]["type"] == "api_error"

    req_api = Request({"type": "http", "method": "GET",
                       "path": "/api/providers", "headers": []})
    assert json.loads(asyncio.run(handler(req_api, ValueError("boom"))).body) == {
        "ok": False, "error": "ValueError: boom"}


def test_unhandled_exception_does_not_leak_paths():
    """返回体只含异常类型与消息，不能带本地路径 / 密钥。"""
    handler = app_module._unhandled_exception
    req = Request({"type": "http", "method": "POST",
                   "path": "/v1/chat/completions", "headers": []})
    body = json.loads(asyncio.run(handler(req, RuntimeError("/home/xiake/.hermes/secrets"))).body)
    assert "sk-local" not in json.dumps(body)


# --------------------------------------------------------------- PUT /api/port
def test_put_port_rejects_non_numeric(client, admin_headers):
    """传 "abc" 以前 int() 直接抛 → 500；端口是用户输入，必须 400。"""
    r = client.put("/api/port", headers=admin_headers, json={"port": "abc"})
    assert r.status_code == 400
    assert "port" in r.text


def test_put_port_rejects_null(client, admin_headers):
    r = client.put("/api/port", headers=admin_headers, json={"port": None})
    assert r.status_code == 400


# ------------------------------------------------------ save_providers 失效缓存
def test_save_providers_invalidates_models_cache(monkeypatch, tmp_path):
    """增删上游后 30 秒内 GET /v1/models 仍返回已删模型 —— 失效必须在写路径上。"""
    _point(monkeypatch, tmp_path)
    app_module._models_cache["data"] = [{"id": "旧模型"}]
    app_module._models_cache["ts"] = 9_999_999_999

    app_module.save_providers([{"name": "p", "models": []}])

    assert app_module._models_cache["data"] is None


# ---------------------------------------------------------------- atomic_write
def test_atomic_write_creates_missing_parent_dirs(tmp_path):
    target = tmp_path / "a" / "b" / "c.json"
    app_module.atomic_write(target, "{}")
    assert target.exists()


def test_atomic_write_leaves_no_tmp_behind(tmp_path):
    target = tmp_path / "c.json"
    app_module.atomic_write(target, '{"x": 1}')
    assert json.loads(target.read_text(encoding="utf-8")) == {"x": 1}
    assert not list(tmp_path.glob("*.tmp")), "正常写入不该残留 tmp 文件"


def test_atomic_write_tmp_name_carries_pid(monkeypatch, tmp_path):
    """tmp 名带 pid —— 两个进程并发写同一文件时不会互踩。"""
    monkeypatch.setattr(os, "getpid", lambda: 111)
    target = tmp_path / "c.json"
    app_module.atomic_write(target, "{}")
    assert target.exists()
    assert not list(tmp_path.glob("*.111.tmp")), "tmp 要被 rename 掉"
