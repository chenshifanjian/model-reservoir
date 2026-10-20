"""配置文件读不了时，软件必须活下来。

config.json / providers.json 都可能被写坏：磁盘满、写入中被杀、杀毒软件拦一道、
从别的机器拷来半截文件。以前 json.loads 直接抛异常 → 模块级崩溃 → 进程静默退出，
用户只看到「程序打不开」，连日志都不一定有（GUI 没起来，日志也可能没落盘）。
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


def _point(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "DATA_FILE", tmp_path / "providers.json")


def test_corrupt_config_falls_back_to_defaults(monkeypatch, tmp_path):
    _point(monkeypatch, tmp_path)
    app_module.CONFIG_FILE.write_text("{ 这不是 json [[[", encoding="utf-8")

    data = app_module.load_config()

    assert isinstance(data, dict)
    assert data["port"] == 8000
    assert data["local_api_key"].startswith("sk-local-")


def test_corrupt_config_is_quarantined_not_deleted(monkeypatch, tmp_path):
    """坏文件必须留档 —— 里面有用户的 key，删了就真没了。"""
    _point(monkeypatch, tmp_path)
    app_module.CONFIG_FILE.write_text("{{{{", encoding="utf-8")

    app_module.load_config()

    # 新的 config.json 会被写成默认值（正常路径），坏内容必须在留档里
    import json as _json
    fresh = _json.loads(app_module.CONFIG_FILE.read_text(encoding="utf-8"))
    assert fresh["port"] == 8000

    leftovers = list(tmp_path.glob("config.json.corrupt-*"))
    assert len(leftovers) == 1
    assert leftovers[0].read_text(encoding="utf-8") == "{{{{", "内容必须原样保留"


def test_config_that_is_json_but_not_an_object(monkeypatch, tmp_path):
    """[] 和 "text" 都是合法 JSON，但都不是对象，一样不能崩。"""
    _point(monkeypatch, tmp_path)
    for payload in ('[]', '"just a string"', "123", "null"):
        app_module.CONFIG_FILE.write_text(payload, encoding="utf-8")
        data = app_module.load_config()
        assert isinstance(data, dict) and data["port"] == 8000, payload


def test_corrupt_config_recovers_local_key_from_backup(monkeypatch, tmp_path):
    """留档的价值就在这里：手工改回去就能恢复原来的 Key。"""
    _point(monkeypatch, tmp_path)
    app_module.CONFIG_FILE.write_text(
        '{"local_api_key": "sk-local-deadbeef", "port": 8001, ', encoding="utf-8")   # 截断

    data = app_module.load_config()
    assert data["port"] == 8000                       # 本次用默认

    backup = next(tmp_path.glob("config.json.corrupt-*"))
    assert '"sk-local-deadbeef"' in backup.read_text(encoding="utf-8"), "旧 key 必须能找回来"


def test_valid_config_still_reads_back_unchanged(monkeypatch, tmp_path):
    """别为了兜底把正常路径也改坏了。"""
    _point(monkeypatch, tmp_path)
    app_module.CONFIG_FILE.write_text(
        '{"local_api_key": "sk-local-ok", "port": 9001, "poll_count": 3, "last_daily_poll": "x"}',
        encoding="utf-8")

    data = app_module.load_config()

    assert data == {"local_api_key": "sk-local-ok", "port": 9001,
                    "poll_count": 3, "last_daily_poll": "x"}
    assert not list(tmp_path.glob("config.json.corrupt-*")), "正常文件不该被挪走"


def test_corrupt_providers_falls_back_to_empty_list(monkeypatch, tmp_path):
    """providers.json 里是全部上游与 Key，一崩了之是最贵的错法。"""
    _point(monkeypatch, tmp_path)
    app_module.DATA_FILE.write_text("[ half a json", encoding="utf-8")

    assert app_module.load_providers() == []
    assert not app_module.DATA_FILE.exists()
    assert len(list(tmp_path.glob("providers.json.corrupt-*"))) == 1


def test_providers_json_not_a_list(monkeypatch, tmp_path):
    _point(monkeypatch, tmp_path)
    app_module.DATA_FILE.write_text('{"oops": 1}', encoding="utf-8")

    assert app_module.load_providers() == []
    assert len(list(tmp_path.glob("providers.json.corrupt-*"))) == 1


def test_valid_providers_read_back_unchanged(monkeypatch, tmp_path):
    _point(monkeypatch, tmp_path)
    app_module.DATA_FILE.write_text('[{"name": "A"}]', encoding="utf-8")

    assert app_module.load_providers() == [{"name": "A"}]
