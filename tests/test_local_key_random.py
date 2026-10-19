import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


def _run(monkeypatch):
    saved = []
    monkeypatch.setattr(app_module, "save_config", lambda: saved.append(dict(app_module.app_config)))
    return saved, asyncio.run(app_module.regenerate_local_key(None))


def test_random_key_rotates_in_memory_and_persists(monkeypatch):
    monkeypatch.setitem(app_module.app_config, "local_api_key", "sk-local-old")
    monkeypatch.setattr(app_module, "LOCAL_API_KEY", "sk-local-old")

    saved, res = _run(monkeypatch)

    key = res["key"]
    assert res["ok"] is True
    assert key.startswith("sk-local-")
    assert len(key) == len("sk-local-") + 32  # 16 字节 = 32 hex
    assert key != "sk-local-old"
    assert app_module.app_config["local_api_key"] == key
    assert app_module.LOCAL_API_KEY == key
    assert saved and saved[-1]["local_api_key"] == key


def test_random_key_is_actually_distinct(monkeypatch):
    monkeypatch.setattr(app_module, "LOCAL_API_KEY", "sk-local-old")
    keys = {_run(monkeypatch)[1]["key"] for _ in range(5)}
    assert len(keys) == 5


def test_random_key_is_hex(monkeypatch):
    _, res = _run(monkeypatch)
    suffix = res["key"][len("sk-local-"):]
    int(suffix, 16)  # 非 hex 会抛 ValueError
    assert suffix == suffix.lower()


# ============================================================
# load_config：补出来的 key 必须落盘
# ============================================================
def test_load_config_persists_generated_key(monkeypatch, tmp_path):
    """config.json 缺 local_api_key 时，补出来的 key 要写回文件。

    否则每次启动都是新的随机 key：文件里没这个字段、UI 看不出变化，
    而所有已对接客户端全部 401 —— 排查起来毫无线索。
    """
    cfg = tmp_path / "config.json"
    cfg.write_text('{"port": 8000}', encoding="utf-8")
    monkeypatch.setattr(app_module, "CONFIG_FILE", cfg)

    first = app_module.load_config()
    assert first["local_api_key"].startswith("sk-local-")
    # 关键：磁盘上已经有了，重启不会换
    on_disk = json.loads(cfg.read_text(encoding="utf-8"))
    assert on_disk["local_api_key"] == first["local_api_key"]

    second = app_module.load_config()
    assert second["local_api_key"] == first["local_api_key"]


def test_load_config_leaves_complete_file_alone(monkeypatch, tmp_path):
    cfg = tmp_path / "config.json"
    cfg.write_text('{"local_api_key": "sk-local-keep", "port": 8000, '
                   '"poll_count": 0, "last_daily_poll": ""}', encoding="utf-8")
    monkeypatch.setattr(app_module, "CONFIG_FILE", cfg)
    assert app_module.load_config()["local_api_key"] == "sk-local-keep"
    assert json.loads(cfg.read_text(encoding="utf-8"))["local_api_key"] == "sk-local-keep"
