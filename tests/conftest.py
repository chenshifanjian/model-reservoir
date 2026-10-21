"""tests/ 共享夹具。

`client` 把数据目录指到临时目录，这样测 PUT /api/config 这类会写盘的接口
不会碰 dist/ 下 live 实例正在用的 config.json。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import pytest
from starlette.testclient import TestClient

import app as app_module

KEY = "sk-local-" + "a1" * 16


@pytest.fixture
def client(tmp_path, monkeypatch):
    monkeypatch.setattr(app_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "DATA_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "ROUTERS_FILE", tmp_path / "routers.json")
    monkeypatch.setattr(app_module, "LOCAL_API_KEY", KEY)
    monkeypatch.setattr(app_module, "ROUTERS", {}, raising=False)
    monkeypatch.setitem(app_module.app_config, "local_api_key", KEY)
    app_module.atomic_write(app_module.DATA_FILE, "[]")
    app_module.atomic_write(app_module.ROUTERS_FILE, "{}")
    return TestClient(app_module.app, raise_server_exceptions=False)


@pytest.fixture
def admin_headers():
    return {"Authorization": f"Bearer {KEY}"}
