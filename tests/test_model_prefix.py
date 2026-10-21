"""回复来源标注开关：默认开，可关，且显式 False 不能被默认值吞掉。"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import app as app_module


def _point(monkeypatch, tmp_path):
    monkeypatch.setattr(app_module, "CONFIG_FILE", tmp_path / "config.json")
    monkeypatch.setattr(app_module, "DATA_FILE", tmp_path / "providers.json")
    monkeypatch.setattr(app_module, "ROUTERS_FILE", tmp_path / "routers.json")


def test_helper_defaults_to_on(monkeypatch):
    """没写过这个字段的老配置，行为必须与加开关前一致 —— 默认开。"""
    monkeypatch.setattr(app_module, "app_config", {"port": 8000})
    assert app_module.show_model_prefix() is True


def test_helper_reads_false(monkeypatch):
    monkeypatch.setattr(app_module, "app_config", {"show_model_prefix": False})
    assert app_module.show_model_prefix() is False


def test_helper_treats_garbage_as_on(monkeypatch):
    """值被写坏时宁可回到开（老行为），也不能让整条回复丢掉来源标注。"""
    for junk in ("", 0, None, "no"):
        monkeypatch.setattr(app_module, "app_config", {"show_model_prefix": junk})
        assert app_module.show_model_prefix() is True, junk


def test_backfill_adds_field_to_old_config(monkeypatch, tmp_path):
    _point(monkeypatch, tmp_path)
    app_module.CONFIG_FILE.write_text(
        '{"local_api_key": "sk-local-x", "port": 8001}', encoding="utf-8")
    data = app_module.load_config()
    assert data["show_model_prefix"] is True
    # 补出来必须落盘，否则每次启动都重新当默认值处理
    import json
    on_disk = json.loads(app_module.CONFIG_FILE.read_text(encoding="utf-8"))
    assert on_disk["show_model_prefix"] is True


def test_backfill_does_not_overwrite_explicit_false(monkeypatch, tmp_path):
    """关键：`x or default` 会把 False 吞成 True。这里判的是字段在不在。"""
    _point(monkeypatch, tmp_path)
    app_module.CONFIG_FILE.write_text(
        '{"local_api_key": "sk-local-x", "port": 8001, "show_model_prefix": false}',
        encoding="utf-8")
    data = app_module.load_config()
    assert data["show_model_prefix"] is False


def test_put_config_toggles_off(client, admin_headers, monkeypatch):
    r = client.put("/api/config", headers=admin_headers,
                   json={"show_model_prefix": False})
    assert r.status_code == 200
    assert r.json()["show_model_prefix"] is False
    assert app_module.show_model_prefix() is False


def test_put_config_toggles_on(client, admin_headers):
    client.put("/api/config", headers=admin_headers, json={"show_model_prefix": False})
    r = client.put("/api/config", headers=admin_headers, json={"show_model_prefix": True})
    assert r.json()["show_model_prefix"] is True
    assert app_module.show_model_prefix() is True


def test_put_config_persists_the_toggle(client, admin_headers, tmp_path):
    client.put("/api/config", headers=admin_headers, json={"show_model_prefix": False})
    import json
    on_disk = json.loads(app_module.CONFIG_FILE.read_text(encoding="utf-8"))
    assert on_disk["show_model_prefix"] is False


def test_put_config_rejects_non_boolean(client, admin_headers):
    """"false" 这种字符串若被存成真值，界面回读会显示成开、用户点关点不动。"""
    for bad in ("false", 0, "no", []):
        r = client.put("/api/config", headers=admin_headers,
                       json={"show_model_prefix": bad})
        assert r.status_code == 400, bad


def test_toggle_does_not_touch_other_fields(client, admin_headers):
    """切开关不能顺手改掉 API Key —— 那会让所有已对接的客户端 401。"""
    before = app_module.app_config.get("local_api_key")
    client.put("/api/config", headers=admin_headers, json={"show_model_prefix": False})
    assert app_module.app_config.get("local_api_key") == before


def test_index_context_carries_the_flag(client):
    """界面回读必须拿到真实开关状态，否则显示的永远是「开」。"""
    r = client.get("/")
    assert r.status_code == 200
    assert 'id="prefixState"' in r.text
    assert (">开<" in r.text) or (">关<" in r.text)
