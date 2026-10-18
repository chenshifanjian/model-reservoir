"""免费档接入回归（学自 dsh-our-free-model：免费渠道 + 不抛弃付费兜底）。"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_apply_preset_adds_no_key_platforms(monkeypatch):
    """needs_key=False 的平台不填 Key 也要能添加 —— 否则卡片是无效控件"""
    import app as A
    from fastapi.testclient import TestClient

    # 用本地 presets，别打网络
    local = json.loads((ROOT / "presets.json").read_text("utf-8"))
    async def _fake_load(_force=False):
        return local
    monkeypatch.setattr(A, "load_preset", _fake_load)

    async def _fake_verify(base_url, key):
        return {"ok": True, "detail": "连接成功"}
    async def _fake_fetch(base_url, key, free_only):
        if "11434" in base_url:      # Ollama
            return ["qwen3:8b", "llama3.2:3b"], {}
        if "1234" in base_url:       # LM Studio
            return ["qwen3:4b"], {}
        return [], {}
    monkeypatch.setattr(A, "verify_provider_key_impl", _fake_verify)
    monkeypatch.setattr(A, "fetch_models_with_pricing", _fake_fetch)

    A.providers.clear()
    client = TestClient(A.app)
    headers = {"Authorization": "Bearer " + A.LOCAL_API_KEY}
    # 一个 Key 都不填 —— 免 Key 平台仍应被添加，要 Key 的应如实报缺
    r = client.post("/api/providers/preset", json={"keys": {}}, headers=headers)
    assert r.status_code == 200, r.text
    res = r.json().get("results", {})

    assert res["Ollama"]["ok"] is True, res.get("Ollama")
    assert res["LM Studio"]["ok"] is True, res.get("LM Studio")
    # 要 Key 的没填，必须仍然失败（不能一起被放行）
    assert res["OpenRouter"]["ok"] is False, res.get("OpenRouter")
    assert "Key" in res["OpenRouter"]["detail"], res["OpenRouter"]

    names = {p["name"] for p in A.providers}
    assert "Ollama" in names, names
    assert "LM Studio" in names, names
    assert "OpenRouter" not in names, names

    om = next(p for p in A.providers if p["name"] == "Ollama")
    assert om["models"] == ["qwen3:8b", "llama3.2:3b"], om
    A.providers.clear()


def test_preset_sources_stay_in_sync():
    """presets.json（远端源）与 BUILTIN_PRESET（离线兜底）必须一致"""
    import app as A
    j = json.loads((ROOT / "presets.json").read_text("utf-8"))["platforms"]
    b = A.BUILTIN_PRESET["platforms"]
    assert set(b) == set(j), set(b) ^ set(j)
    for name in b:
        for field in ("base_url", "needs_key", "free_quota"):
            assert b[name].get(field) == j[name].get(field), (name, field)
    # 至少要有免 Key 平台，否则「免 Key 开箱即用」整组是空谈
    assert any(c.get("needs_key") is False for c in j.values())
