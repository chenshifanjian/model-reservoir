"""免费档接入回归（学自 dsh-our-free-model：免费渠道 + 不抛弃付费兜底）。"""
import json
import pathlib

ROOT = pathlib.Path(__file__).resolve().parent.parent


def test_apply_preset_adds_no_key_platforms(monkeypatch):
    """needs_key=False 的平台不填 Key 也要能添加 —— 否则卡片是无效控件。

    **用合成夹具，不读真实 presets.json**：Ollama / LM Studio 已于 2026-10-20
    按用户要求从预设移除（见 test_removed_platforms_stay_removed），但
    `needs_key=false` 这条后端机制仍在 —— 将来要重新引入本机推理平台时它得是好的。
    机制与预设内容是两件事，分开测。
    """
    import app as A
    from fastapi.testclient import TestClient

    synthetic = {"platforms": {
        "LocalBox": {"base_url": "http://127.0.0.1:19999/v1",
                     "needs_key": False, "free_only": False, "models": []},
        "OpenRouter": {"base_url": "https://openrouter.ai/api/v1",
                       "needs_key": True, "free_only": False, "models": []},
    }}
    async def _fake_load(_force=False):
        return synthetic
    monkeypatch.setattr(A, "load_preset", _fake_load)

    async def _fake_verify(base_url, key):
        return {"ok": True, "detail": "连接成功"}
    async def _fake_fetch(base_url, key, free_only):
        if "19999" in base_url:
            return ["qwen3:8b", "llama3.2:3b"], {}
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

    assert res["LocalBox"]["ok"] is True, res.get("LocalBox")
    # 要 Key 的没填，必须仍然失败（不能一起被放行）
    assert res["OpenRouter"]["ok"] is False, res.get("OpenRouter")
    assert "Key" in res["OpenRouter"]["detail"], res["OpenRouter"]

    names = {p["name"] for p in A.providers}
    assert "LocalBox" in names, names
    assert "OpenRouter" not in names, names

    lb = next(p for p in A.providers if p["name"] == "LocalBox")
    assert lb["models"] == ["qwen3:8b", "llama3.2:3b"], lb
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


def test_removed_platforms_stay_removed():
    """2026-10-20 用户要求移除「免 Key，开箱即用」（Ollama / LM Studio）。

    两个来源都必须干净：presets.json 是远端热更新的源（改完就推、客户端长 TTL 后生效），
    BUILTIN_PRESET 是断网兜底。远端是可热改的，所以这条护栏必须常驻 —— 
    否则谁在 presets.json 里加回一行，界面就会重新长出这一组，而且没人会注意到。
    """
    import app as A
    j = json.loads((ROOT / "presets.json").read_text("utf-8"))["platforms"]
    b = A.BUILTIN_PRESET["platforms"]
    for src_name, src in (("presets.json", j), ("BUILTIN_PRESET", b)):
        for gone in ("Ollama", "LM Studio"):
            assert gone not in src, f"{src_name} 里 {gone} 又回来了"
        # 整组应该已经没有免 Key 平台；若将来重新引入，需同步改这条
        assert not any(c.get("needs_key") is False for c in src.values()), \
            f"{src_name} 又出现了 needs_key=False 的平台（若确要恢复免 Key 组，先改这条断言）"


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


def test_preset_new_key_coexists_instead_of_overwriting(monkeypatch):
    """同一个平台填了不同的 Key → 新增一条并存，旧的原封不动

    用户口径：「哪怕是一个上游提供商，只要是 api key 不一样就不要覆盖」。
    团队里每人各自注册免费账号，覆盖等于把别人的额度顶掉。
    """
    import app as A
    from fastapi.testclient import TestClient

    async def _fake_load(_force=False):
        return {"platforms": {
            "Groq": {"base_url": "https://api.groq.com/openai/v1",
                     "needs_key": True, "free_only": False, "models": []},
        }}

    async def _fake_verify(base_url, key):
        return {"ok": True, "detail": "连接成功"}

    async def _fake_fetch(base_url, key, free_only):
        return ["m-" + key[-4:]], {}
    monkeypatch.setattr(A, "load_preset", _fake_load)
    monkeypatch.setattr(A, "verify_provider_key_impl", _fake_verify)
    monkeypatch.setattr(A, "fetch_models_with_pricing", _fake_fetch)

    A.providers.clear()
    A.providers.append({"name": "Groq", "base_url": "https://api.groq.com/openai/v1",
                        "api_key": "sk-aaaa", "models": ["m-aaaa"], "free_only": False})

    client = TestClient(A.app)
    h = {"Authorization": "Bearer " + A.LOCAL_API_KEY}
    r = client.post("/api/providers/preset",
                    json={"keys": {"Groq": "sk-bbbb"}}, headers=h)
    res = r.json()["results"]

    assert len(A.providers) == 2, [p["name"] for p in A.providers]
    keys = sorted(p["api_key"] for p in A.providers)
    assert keys == ["sk-aaaa", "sk-bbbb"], keys
    # 旧条目连名字都没被动过
    assert any(p["name"] == "Groq" and p["api_key"] == "sk-aaaa" for p in A.providers)
    # 新条目带后缀
    assert any(p["name"] != "Groq" and p["api_key"] == "sk-bbbb" for p in A.providers)
    detail = res["Groq"]["detail"]
    assert ("并存" in detail) or ("新增" in detail), detail
    A.providers.clear()
