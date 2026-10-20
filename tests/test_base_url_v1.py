"""上游 base_url 的 /v1 归一化。

这条线挡的是一个很具体的坑：只在 `/v1` 下提供服务的 OpenAI 兼容服务器
（llama.cpp、Ollama 兼容层、各类自建推理服务），用户按直觉填根地址时
`/models` 会 404，而以前的报错说「API Key 校验失败」——把路径问题说成
key 的问题，用户只会反复换 key。
"""
import asyncio

import app as app_module


class _Resp:
    def __init__(self, status):
        self.status_code = status


class _FakeHTTP:
    """按路径返回不同状态码，用来模拟「根路径没有 /v1 才有」的服务。"""

    def __init__(self, routes):
        self._routes = routes        # {后缀: status}
        self.calls = []

    async def get(self, url, headers=None, timeout=None):
        self.calls.append(url)
        # 长后缀优先：`/v1/models` 也以 `/models` 结尾，
        # 按 dict 插入顺序匹配会先把 /v1 那条吃掉，测不出真实分支。
        for suffix, status in sorted(self._routes.items(),
                                     key=lambda kv: -len(kv[0])):
            if url.endswith(suffix):
                return _Resp(status)
        return _Resp(404)


def _verify(monkeypatch, routes, base_url):
    fake = _FakeHTTP(routes)
    monkeypatch.setattr(app_module, "http_client", fake)
    return asyncio.run(app_module.verify_provider_key_impl(base_url, "sk-test")), fake


def test_root_path_works(monkeypatch):
    """根地址本来就能用时，不加 /v1，也不改 base_url。"""
    vr, fake = _verify(monkeypatch, {"/models": 200}, "http://h:8080")
    assert vr["ok"] is True
    assert vr["base_url"] == "http://h:8080"
    assert len(fake.calls) == 1          # 没有多余的探测请求


def test_v1_only_server_gets_normalized(monkeypatch):
    """根路径 404、/v1 可用 → 自动补全，base_url 变成带 /v1 的形式。"""
    vr, fake = _verify(monkeypatch, {"/v1/models": 200}, "http://h:8080/")
    assert vr["ok"] is True
    assert vr["base_url"] == "http://h:8080/v1"
    assert "/v1" in vr["detail"]         # 前端会把这个 detail 显示给用户
    assert len(fake.calls) == 2


def test_trailing_slash_stripped_before_normalize(monkeypatch):
    vr, _ = _verify(monkeypatch, {"/v1/models": 200}, "http://h:8080/")
    assert vr["base_url"] == "http://h:8080/v1"    # 不能出现 //v1


def test_both_404_explains_path_not_key(monkeypatch):
    """两个都 404 时，报错必须指向路径而不是 key。"""
    vr, _ = _verify(monkeypatch, {}, "http://h:8080")
    assert vr["ok"] is False
    assert "404" in vr["detail"]
    assert "/v1" in vr["detail"]         # 告诉用户该怎么填
    assert "Key 无效" not in vr["detail"]  # 不能甩锅给 key


def test_v1_401_reports_bad_key(monkeypatch):
    """/v1 也在，但 401 → 这才是真的 key 错。"""
    vr, _ = _verify(monkeypatch, {"/models": 404, "/v1/models": 401},
                    "http://h:8080")
    assert vr["ok"] is False
    assert "Key 无效" in vr["detail"]
    assert "401" in vr["detail"]


def test_root_401_still_reports_bad_key(monkeypatch):
    """根路径直接 401 时不该再去试 /v1 —— key 已经确定是坏的。"""
    vr, fake = _verify(monkeypatch, {"/models": 401}, "http://h:8080")
    assert vr["ok"] is False
    assert "Key 无效" in vr["detail"]
    assert len(fake.calls) == 1


def test_other_error_codes_pass_through(monkeypatch):
    vr, _ = _verify(monkeypatch, {"/models": 503}, "http://h:8080")
    assert vr["ok"] is False
    assert "503" in vr["detail"]


def test_add_provider_saves_normalized_base_url(monkeypatch):
    """add_provider 必须把归一化后的 base_url 存下来 —— 
    否则下游拼 /chat/completions 时仍然打到根路径。"""
    saved = []

    fake = _FakeHTTP({"/v1/models": 200})
    monkeypatch.setattr(app_module, "http_client", fake)
    monkeypatch.setattr(app_module, "providers", [])
    monkeypatch.setattr(app_module, "save_providers", lambda d: saved.append(list(d)))

    class _In:
        name = "LocalQwen"
        base_url = "http://10.22.74.156:8080"
        api_key = "sk-test"
        models = ["qwen3.8-27b"]
        free_only = True
        free_status_override = None
        model_pricing = None

        def model_dump(self):
            return {"name": self.name, "base_url": self.base_url,
                    "api_key": self.api_key, "models": self.models}

    asyncio.run(app_module.add_provider(_In()))

    assert saved, "providers 没被保存"
    assert saved[0][0]["base_url"] == "http://10.22.74.156:8080/v1"
