"""共享池（双向同步）回归。

用户需求原话：「队长与队员之间上游供应商的配置双向同步，且发现 api key
不同时不覆盖（哪怕是一个上游供应商，也要保存不同的 api，当作是不同的
上游供应商看待）」。
"""
import sys
import pathlib

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent))

import team_client as TC  # noqa: E402


def _p(name, base, key, team=None):
    p = {"name": name, "base_url": base, "api_key": key, "models": ["m"]}
    if team:
        p[TC.TEAM_ORIGIN_KEY] = {"team_id": team, "captain": "队长", "synced_at": 0}
        p[TC.TEAM_STATUS_KEY] = "active"
    return p


def test_same_team_same_name_different_key_coexists():
    """同队同名但 key 不同 → 并存，绝不覆盖

    团队里每人各自注册免费账号，同名不同 key 若被覆盖，
    等于把别人的额度顶掉 —— 而聚合额度正是组队的初衷。
    """
    mine = _p("Groq", "https://api.groq.com/openai/v1", "sk-mine")
    providers = [mine]

    incoming = {"name": "Groq", "base_url": "https://api.groq.com/openai/v1",
                "api_key": "sk-teammate", "models": ["m2"]}
    providers, added, note = TC.merge_team_provider(
        providers, incoming, "team-1", "张三", contributor="mem-zhang")

    assert added is True
    names = [p["name"] for p in providers]
    assert len(providers) == 2, names
    # 我自己的那条原封不动
    assert providers[0]["api_key"] == "sk-mine", providers[0]
    # 新来的那条并存，名字带贡献者后缀
    assert providers[1]["api_key"] == "sk-teammate", providers[1]
    assert providers[1]["name"] != "Groq", providers[1]["name"]


def test_same_team_same_name_same_key_updates():
    """同队同名且 key 相同 → 才算同一条的更新（队长改设置重新推送）"""
    providers = [_p("Groq", "https://api.groq.com/openai/v1", "sk-mine", team="team-1")]
    incoming = {"name": "Groq", "base_url": "https://api.groq.com/openai/v1",
                "api_key": "sk-mine", "models": ["m-new"]}
    providers, added, note = TC.merge_team_provider(providers, incoming, "team-1", "队长")

    assert added is True
    assert len(providers) == 1, [p["name"] for p in providers]
    assert providers[0]["models"] == ["m-new"], providers[0]


def test_contributable_excludes_team_origin():
    """来自队伍的上游不回传 —— 否则队里会无限回声"""
    providers = [
        _p("我的", "https://a.example/v1", "sk-a"),
        _p("队伍来的", "https://b.example/v1", "sk-b", team="team-1"),
        _p("另一队来的", "https://c.example/v1", "sk-c", team="team-2"),
    ]
    got = TC._contributable(providers, "team-1")
    names = [p["name"] for p in got]
    assert names == ["我的"], names


def test_allowlist_default_open():
    """白名单默认全开：不传 / 传空 / 没写到的平台都同步；显式 False 才关"""
    import asyncio

    providers = [_p("OpenRouter", "https://openrouter.ai/api/v1", "sk-or"),
                 _p("Groq", "https://api.groq.com/openai/v1", "sk-gq")]

    calls = []

    class _FakeState:
        client_id = "me"

        def list_teams(self):
            return {"team-1": {}}

        def get_team(self, team_id):
            return {"code": "ABCDEFGH", "code_salt": "ab" * 32, "token": "tok",
                    "status": "approved"}

    async def _fake_req(state, method, path, body=None, token=None, timeout=20.0):
        calls.append(body["provider_name"])
        return {"ok": True}

    orig = TC._req
    TC._req = _fake_req
    try:
        # 白名单全空 → 两条都传
        calls.clear()
        asyncio.run(TC.push_contributions(_FakeState(), providers, {}))
        assert sorted(calls) == ["Groq", "OpenRouter"], calls

        # 显式关掉 Groq → 只传 OpenRouter
        calls.clear()
        asyncio.run(TC.push_contributions(
            _FakeState(), providers, {"Groq": False}))
        assert calls == ["OpenRouter"], calls
    finally:
        TC._req = orig
