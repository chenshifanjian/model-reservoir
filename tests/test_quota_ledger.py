"""quota.py 的账本语义。

这里测的是「什么证据允许我们避让一个候选」——这条线划错的后果不对称：
放宽了只是多撞一次 429，收紧了会把可用模型悄悄关掉，而且没有任何界面会告诉用户。
所以断言重点放在「没有证据就绝不能拦」。
"""
import time

import pytest

import quota


@pytest.fixture(autouse=True)
def _clean():
    quota.reset_all()
    yield
    quota.reset_all()


def test_no_evidence_never_blocks():
    """没头、没 429 → 永远放行。绝不能凭空设一个上限。"""
    quota.observe("prov", "m", status=200, headers={})
    assert quota.cooldown_left("prov", "m") == 0.0
    assert not quota.is_cooling("prov", "m")


def test_429_starts_ladder_and_success_steps_back():
    quota.observe("prov", "m", status=429, headers={}, message="too many requests")
    assert quota.is_cooling("prov", "m")
    left = quota.cooldown_left("prov", "m")
    assert 85 <= left <= 95, left          # 阶梯第一格 90s

    quota.observe("prov", "m", status=200, headers={}, message="")
    assert quota.cooldown_left("prov", "m") == 0.0
    # 退一格但没清零：连续撞两次的账号仍要被记住
    quota.observe("prov", "m", status=429, headers={}, message="too many requests")
    assert quota.is_cooling("prov", "m")


def test_ladder_escalates_and_caps():
    for i, expect_lo in enumerate((85, 115, 590, 3590)):
        quota.reset_all()
        for _ in range(i + 1):
            quota.observe("prov", "m", status=429, headers={}, message="slow down")
        left = quota.cooldown_left("prov", "m")
        assert left >= expect_lo, (i, left)
        assert left <= expect_lo + 10, (i, left)
    # 顶到 86400 后不再涨
    for _ in range(12):
        quota.observe("prov", "m", status=429, headers={}, message="slow down")
    assert quota.cooldown_left("prov", "m") <= 86410


def test_retry_after_header_wins_when_longer():
    quota.observe("prov", "m", status=429,
                  headers={"Retry-After": "600"}, message="slow down")
    assert 590 <= quota.cooldown_left("prov", "m") <= 610


def test_daily_quota_waits_until_utc_midnight():
    """文案点名每日额度 → 等到 UTC 午夜，而不是退一格 90 秒。"""
    before = quota._seconds_to_utc_midnight()
    quota.observe("prov", "m", status=429, headers={},
                  message="Rate limit reached for requests per day on this model")
    left = quota.cooldown_left("prov", "m")
    assert left >= before * 0.99, (left, before)
    assert left <= before + 5


def test_rate_limit_headers_feed_both_scopes():
    h = {
        "x-ratelimit-limit-requests": "30",
        "x-ratelimit-remaining-requests": "17",
        "x-ratelimit-reset-requests": "6m0s",
        "x-ratelimit-remaining-tokens": "0",
    }
    quota.observe("prov", "m", status=200, headers=h)
    # 账号层与模型层都记到了
    rows = {r["scope"]: r for r in quota.snapshot()}
    assert rows["a:prov"]["requests_remaining"] == 17
    assert rows["m:prov||m"]["requests_remaining"] == 17
    # token 剩余 0 → 该作用域进入冷却
    assert rows["a:prov"]["cooling"] is True


def test_account_level_cooling_covers_model():
    """账号额度用尽时，账号下的每个模型都要跟着避让。"""
    quota.force_cooldown("prov", None, 120, reason="test")
    assert quota.is_cooling("prov", "任意模型")
    assert quota.is_cooling("prov", "另一个模型")


def test_model_cooling_does_not_leak_to_sibling():
    quota.force_cooldown("prov", "m1", 120, reason="test")
    assert quota.is_cooling("prov", "m1")
    assert not quota.is_cooling("prov", "m2")


def test_reset_value_formats():
    now = time.time()
    for v, lo, hi in (
        ("60", now + 55, now + 65),            # 时长
        ("6m0s", now + 350, now + 370),        # go duration
        ("PT6M", now + 350, now + 370),        # ISO8601 duration
        ("3600", now + 3500, now + 3700),
        ("1792500000", 1792500000 - 1, 1792500000 + 1),  # unix 秒
    ):
        at = quota._parse_reset_value(v)
        assert at is not None, v
        assert lo <= at <= hi, (v, at)


def test_reset_value_unparseable_is_none():
    for v in ("", "soon", "tomorrow-ish", "N/A"):
        assert quota._parse_reset_value(v) is None, v


def test_retry_after_http_date():
    at = quota._parse_retry_after(
        "Wed, 21 Oct 2099 07:28:00 GMT")
    assert at is not None and at > 1e9


def test_note_counts_without_evidencing():
    """note() 只计数，不产生任何额度证据。"""
    quota.note("prov", "m")
    assert quota.is_cooling("prov", "m") is False
    rows = {r["scope"]: r for r in quota.snapshot()}
    assert rows["m:prov||m"]["cooling"] is False
    assert rows["m:prov||m"]["requests_remaining"] is None
    assert rows["m:prov||m"]["last_429_at"] == 0.0


def test_snapshot_hides_nothing_sensitive():
    quota.force_cooldown("prov", "m", 30, reason="test")
    rows = {r["scope"]: r for r in quota.snapshot()}
    assert "m:prov||m" in rows
    assert rows["m:prov||m"]["cooldown_left"] > 0
    # 不该出现任何 key 类字段
    for row in rows.values():
        assert "key" not in row and "api" not in row
