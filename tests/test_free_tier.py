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


def test_auth_headers_omits_header_for_empty_key():
    """空 Key 不能拼 'Bearer '（末尾空格）—— httpx 会抛 Illegal header value

    本机推理服务（Ollama / LM Studio）不需要鉴权，免 Key 平台的 Key 就是空串。
    """
    import app as A
    assert A.auth_headers("") == {}
    assert A.auth_headers(None) == {}
    assert A.auth_headers("   ") == {}          # 纯空白同样视为无
    assert A.auth_headers("sk-x") == {"Authorization": "Bearer sk-x"}

def test_preset_skips_configured_by_default(monkeypatch):
    """一键配置默认不动已配置的平台 —— 团队场景下覆盖会抹掉个人手调设置

    团队每人各自注册免费账号、各自手调 free_only 与模型禁用状态；
    留空 Key 的老逻辑会拿旧 Key 重建条目，那是回退而非保留。
    """
    import app as A
    from fastapi.testclient import TestClient

    async def _fake_load(_force=False):
        return {"platforms": {
            "Ollama": {"base_url": "http://127.0.0.1:11434/v1",
                       "needs_key": False, "free_only": False, "models": []},
            "Groq": {"base_url": "https://api.groq.com/openai/v1",
                     "needs_key": True, "free_only": False, "models": []},
        }}
    async def _fake_verify(base_url, key):
        return {"ok": True, "detail": "连接成功"}
    async def _fake_fetch(base_url, key, free_only):
        return ["fresh-model"], {}
    monkeypatch.setattr(A, "load_preset", _fake_load)
    monkeypatch.setattr(A, "verify_provider_key_impl", _fake_verify)
    monkeypatch.setattr(A, "fetch_models_with_pricing", _fake_fetch)

    # 预置一个「已配置」的 Ollama，模型列表是旧的，且关掉了 free_only
    A.providers.clear()
    A.providers.append({"name": "Ollama", "base_url": "http://127.0.0.1:11434/v1",
                        "api_key": "", "models": ["old-model"], "free_only": True,
                        "disabled": ["old-model"]})

    client = TestClient(A.app)
    h = {"Authorization": "Bearer " + A.LOCAL_API_KEY}

    # 默认（不传 overwrite_existing）：已配置的必须原样不动。
    # 同时给 Groq 一个 Key，验证「没配过的仍然会被处理」——
    # 跳过逻辑只作用于已配置的，不能误伤未配置的。
    r = client.post("/api/providers/preset",
                    json={"keys": {"Groq": "sk-test"}}, headers=h)
    res = r.json()["results"]
    om = next(p for p in A.providers if p["name"] == "Ollama")
    assert res["Ollama"]["ok"] is True, res["Ollama"]
    assert "跳过" in res["Ollama"]["detail"], res["Ollama"]
    assert om["models"] == ["old-model"], om["models"]      # 没被刷新
    assert om["free_only"] is True, om["free_only"]          # 手调设置没被抹掉
    assert om["disabled"] == ["old-model"], om["disabled"]   # 禁用状态没被抹掉
    # Groq 没配过，应该真的去配
    assert "Groq" in {p["name"] for p in A.providers}

    # 显式勾选覆盖：才允许重建
    A.providers[:] = [p for p in A.providers if p["name"] == "Ollama"]
    r = client.post("/api/providers/preset",
                    json={"keys": {}, "overwrite_existing": True}, headers=h)
    res = r.json()["results"]
    om = next(p for p in A.providers if p["name"] == "Ollama")
    assert om["models"] == ["fresh-model"], om["models"]     # 这次真的重建了
    A.providers.clear()


def test_preset_skip_names_team_origin(monkeypatch):
    """组队同步来的上游要被点名 —— 覆盖它会破坏同步状态"""
    import app as A
    from fastapi.testclient import TestClient

    async def _fake_load(_force=False):
        return {"platforms": {
            "OpenRouter": {"base_url": "https://openrouter.ai/api/v1",
                           "needs_key": True, "free_only": False, "models": []},
        }}
    monkeypatch.setattr(A, "load_preset", _fake_load)

    A.providers.clear()
    A.providers.append({"name": "OpenRouter", "base_url": "https://openrouter.ai/api/v1",
                        "api_key": "sk-team", "models": ["m"], "team_origin": "工程队"})

    client = TestClient(A.app)
    h = {"Authorization": "Bearer " + A.LOCAL_API_KEY}
    r = client.post("/api/providers/preset", json={"keys": {}}, headers=h)
    res = r.json()["results"]
    assert "工程队" in res["OpenRouter"]["detail"], res["OpenRouter"]
    A.providers.clear()
