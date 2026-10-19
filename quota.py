"""免费档配额账本：按 (提供方, 模型) 与 (提供方账号) 记额度消耗与冷却。

熔断（app.py 的 circuit_breaker）回答的是「这个候选刚才坏没坏」；
账本回答的是「这个候选的额度还剩多少」。免费平台的日限额一旦撞上，
接下来几个小时再怎么重试都是 429 —— 那不是故障，是额度用完了，
要按配额窗口的重置时间主动避让，而不是等熔断反复开关、反复撞墙。

三条设计约束：

1. **只认证据，不猜上限。** 证据只有两种：上游的 X-RateLimit-* 头（精确）、
   429 响应（粗略，靠阶梯退避）。两者都没有时当无限额度放行，
   宁可让客户端撞一次 429，也不能凭一个编出来的上限把模型拦死。
2. **作用域分两层。** 单个 (提供方, 模型)，以及整个提供方账号。
   OpenRouter / Google 这类平台的限额是按账号算的 —— 单个模型没限，
   但账号总量有限；只看模型层会得出「还有额度」的错误结论。
3. **只影响排序，不移除候选。** 全部候选都冷却中时照样把它们返回出去，
   让请求有机会撞一次 —— 额度可能已经悄悄重置了，而我们并不知道。

线程安全：请求处理与后台探测会并发写，所有读写都过一把锁。
"""

from __future__ import annotations

import email.utils
import re
import threading
import time
from datetime import datetime, timedelta, timezone

# 429 且没有任何可读重置信息时的退避阶梯。每次撞 429 进一格，
# 成功一次退回第一格 —— 免费档的限流是「一阵一阵」的，退避不该永久保留。
COOLDOWN_LADDER = (90.0, 120.0, 600.0, 3600.0, 86400.0)

# 从 429 响应文案里判断「额度用完了」还是「瞬间打太密」。
# 前者要等到日窗口重置（可能几小时），后者退一格就够（90 秒）。
_DAY_MARKERS = (
    "per day", "daily", "day quota", "quota exceeded", "quotaexceeded",
    "day_limit", "day limit", "requests per day", "tokens per day",
    "每日", "日限额", "按天",
)
_TOKEN_MARKERS = ("token", "tpm", "上下文长度", "too long", "context_length_exceeded")
# ISO 8601 duration 与 go 的 time.ParseDuration 产物都可能出现
_ISO_DUR = re.compile(
    r"^P(?:(?P<d>\d+(?:\.\d+)?)D)?"
    r"(?:T(?:(?P<h>\d+(?:\.\d+)?)H)?(?:(?P<m>\d+(?:\.\d+)?)M)?"
    r"(?:(?P<s>\d+(?:\.\d+)?)S)?)?$", re.I)
_GO_DUR = re.compile(
    r"^(?:(?P<h>\d+(?:\.\d+)?)h)?(?:(?P<m>\d+(?:\.\d+)?)m(?!s))?"
    r"(?:(?P<s>\d+(?:\.\d+)?)s)?(?:(?P<ms>\d+(?:\.\d+)?)ms)?$", re.I)


class QuotaLedger:
    """额度账本。一个进程一份即可，测试里可以另建实例。"""

    def __init__(self, ladder=COOLDOWN_LADDER):
        self._ladder = tuple(ladder)
        self._lock = threading.Lock()
        self._scopes: dict[str, dict] = {}

    # ---------------- 作用域 ----------------
    @staticmethod
    def account_scope(provider: str) -> str:
        return f"a:{provider}"

    @staticmethod
    def model_scope(provider: str, model: str) -> str:
        return f"m:{provider}||{model}"

    def scopes_for(self, provider: str, model: str | None = None) -> list[str]:
        """账号层 + 模型层。模型为空时只有账号层（探测只证明了账号还活着）。"""
        out = [self.account_scope(provider)]
        if model:
            out.append(self.model_scope(provider, model))
        return out

    def _entry(self, scope: str) -> dict:
        e = self._scopes.get(scope)
        if e is None:
            e = {
                "cooldown_until": 0.0,
                "step": 0,          # 阶梯位置
                "last_429_at": 0.0,
                # {"limit": None, "remaining": None, "reset_at": None}
                "req": {},
                "tok": {},
                "window_started": 0.0,
                "window_count": 0,
            }
            self._scopes[scope] = e
        return e

    # ---------------- 写 ----------------
    def observe(self, provider: str, model: str | None = None, *,
                status: int | None = None, headers=None,
                retry_after=None, message: str = "") -> None:
        """记录一次上游交互的结果。headers 传 dict 或 httpx.Headers 都行。"""
        hdr = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}
        retry = retry_after if retry_after is not None else _parse_retry_after(
            hdr.get("retry-after"))
        msg_l = (message or "").lower()

        with self._lock:
            for scope in self.scopes_for(provider, model):
                e = self._entry(scope)
                self._eat_headers(e, hdr)
                if status == 429:
                    e["last_429_at"] = time.time()
                    # 阶梯进一格；文案里若点名了「每日/配额」就直接等到日窗口重置
                    wait = self._ladder[min(e["step"], len(self._ladder) - 1)]
                    e["step"] = min(e["step"] + 1, len(self._ladder) - 1)
                    if _is_daily_quota(msg_l):
                        wait = max(wait, _seconds_to_utc_midnight())
                    if retry is not None:
                        wait = max(wait, retry)
                    e["cooldown_until"] = max(e["cooldown_until"], time.time() + wait)
                elif status is not None and 200 <= status < 300:
                    # 成功过一次，说明上一轮的额度障碍已经不在了，退一格。
                    # 不整体清零：一天里连撞两次 429 的账号确实该被记住。
                    if e["step"]:
                        e["step"] -= 1
                    if not _is_rate_limited(hdr, msg_l):
                        e["cooldown_until"] = 0.0
                self._bump_window(e)

    def force_cooldown(self, provider: str, model: str | None, seconds: float,
                       reason: str = "") -> None:
        """显式冷却**一个**作用域。

        传了 model 就只冷这一个模型 —— 本地判定「这个模型额度用尽」时，
        顺手把整个账号下的兄弟模型一起关掉是错的：账号额度和模型额度是
        两回事，很多平台恰恰是模型有限额而账号无限制（反之亦然）。
        只有不传 model 才是账号级冷却。
        """
        scope = self.model_scope(provider, model) if model else self.account_scope(provider)
        with self._lock:
            e = self._entry(scope)
            e["cooldown_until"] = max(
                e["cooldown_until"], time.time() + float(seconds))
            if reason:
                e["last_reason"] = reason

    def note(self, provider: str, model: str | None = None) -> None:
        """记一次出站请求（供 RPM 窗口统计）。不带状态，不算证据。"""
        with self._lock:
            for scope in self.scopes_for(provider, model):
                self._bump_window(self._entry(scope))

    # ---------------- 读 ----------------
    def cooldown_left(self, provider: str, model: str | None = None) -> float:
        """还要避让多少秒。0 表示可以正常试。"""
        now = time.time()
        left = 0.0
        with self._lock:
            for scope in self.scopes_for(provider, model):
                e = self._scopes.get(scope)
                if not e:
                    continue
                left = max(left, e["cooldown_until"] - now)
        return left

    def is_cooling(self, provider: str, model: str | None = None) -> bool:
        return self.cooldown_left(provider, model) > 0

    def snapshot(self, provider: str | None = None) -> list[dict]:
        """给监控页/排查用：列出所有有记录的作用域。不暴露任何 key。"""
        now = time.time()
        out = []
        with self._lock:
            for scope, e in self._scopes.items():
                if provider and not scope.endswith(f":{provider}") and provider not in scope:
                    continue
                left = e["cooldown_until"] - now
                out.append({
                    "scope": scope,
                    "cooling": left > 0,
                    "cooldown_left": round(max(0.0, left), 1),
                    "step": e["step"],
                    "last_429_at": e["last_429_at"],
                    "requests_remaining": e["req"].get("remaining"),
                    "requests_reset_in": _reset_in(e["req"], now),
                    "tokens_remaining": e["tok"].get("remaining"),
                })
        return sorted(out, key=lambda x: (not x["cooling"], x["scope"]))

    def reset_all(self) -> None:
        with self._lock:
            self._scopes.clear()

    # ---------------- 内部 ----------------
    @staticmethod
    def _bump_window(e: dict):
        now = time.time()
        if now - e["window_started"] >= 60.0:
            e["window_started"] = now
            e["window_count"] = 0
        e["window_count"] += 1

    @staticmethod
    def _eat_headers(e: dict, hdr: dict):
        """把 X-RateLimit-* 家族吃进账本。认不出的头一律忽略。"""
        for bucket, keys in (
            ("req", ("limit-requests", "limit-request")),
            ("tok", ("limit-tokens", "limit-token")),
        ):
            limit = _first_int(hdr, (f"x-ratelimit-{k}" for k in keys))
            if limit is None:
                limit = _first_int(hdr, ("anthropic-ratelimit-requests-limit",)
                                   if bucket == "req"
                                   else ("anthropic-ratelimit-tokens-limit",))
            if limit is None:
                continue
            e.setdefault(bucket, {})
            e[bucket]["limit"] = limit

        for bucket, keys in (
            ("req", ("remaining-requests", "remaining-request")),
            ("tok", ("remaining-tokens", "remaining-token")),
        ):
            rem = _first_int(hdr, (f"x-ratelimit-{k}" for k in keys))
            if rem is None:
                rem = _first_int(hdr, ("anthropic-ratelimit-requests-remaining",)
                                 if bucket == "req"
                                 else ("anthropic-ratelimit-tokens-remaining",))
            if rem is None:
                continue
            e.setdefault(bucket, {})
            e[bucket]["remaining"] = rem
            # 上游明确说「没额度了」→ 按重置时间避让
            if rem <= 0:
                e["cooldown_until"] = max(
                    e["cooldown_until"],
                    time.time() + 60.0)

        for bucket, keys in (
            ("req", ("reset-requests", "reset-request")),
            ("tok", ("reset-tokens", "reset-token")),
        ):
            rs = _first_str(hdr, (f"x-ratelimit-{k}" for k in keys))
            if rs is None:
                continue
            at = _parse_reset_value(rs)
            if at is not None:
                e.setdefault(bucket, {})
                e[bucket]["reset_at"] = at


# ============================================================
# 解析助手（模块级，方便单独测）
# ============================================================
def _first_int(hdr: dict, keys) -> int | None:
    for k in keys:
        v = hdr.get(k)
        if v is None:
            continue
        try:
            return int(str(v).strip())
        except (TypeError, ValueError):
            m = re.search(r"-?\d+", str(v))
            if m:
                return int(m.group())
    return None


def _first_str(hdr: dict, keys) -> str | None:
    for k in keys:
        v = hdr.get(k)
        if v:
            return str(v).strip()
    return None


def _parse_retry_after(value):
    """Retry-After：秒数、HTTP-date 或 go duration。认不出返回 None。"""
    if not value:
        return None
    s = str(value).strip()
    try:
        return max(0.0, float(s))
    except ValueError:
        pass
    at = _parse_reset_value(s)
    if at is not None:
        return max(0.0, at - time.time())
    return None


def _parse_reset_value(value):
    """把各种形态的重置时间/时长统一成 unix 时间戳。认不出返回 None。"""
    s = str(value).strip()
    if not s:
        return None
    now = time.time()
    # 纯数字：可能是秒数时长，也可能是 unix 时间戳
    try:
        n = float(s)
    except ValueError:
        n = None
    if n is not None:
        if n <= 0:
            return None
        if n < 10_000_000:          # 明显是时长（< 1970-04）
            return now + n
        if n < 100_000_000_000:     # 秒级时间戳
            return n
        return n / 1000.0           # 毫秒级时间戳
    # ISO 8601 duration：PT6M / P0D...
    m = _ISO_DUR.match(s)
    if m and any(m.groupdict().values()):
        return now + _dur_seconds(m.groupdict())
    # go duration：6m0s / 1h2m3s
    m = _GO_DUR.match(s)
    if m and any(m.groupdict().values()):
        return now + _dur_seconds(m.groupdict())
    # HTTP-date
    try:
        dt = email.utils.parsedate_to_datetime(s)
        if dt is not None:
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
    except (TypeError, ValueError, IndexError):
        pass
    # ISO 8601 时刻：2026-10-19T03:00:00Z
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        pass
    return None


def _dur_seconds(parts: dict) -> float:
    tot = 0.0
    for k, mul in (("d", 86400.0), ("h", 3600.0), ("m", 60.0), ("s", 1.0), ("ms", 0.001)):
        v = parts.get(k)
        if v:
            tot += float(v) * mul
    return tot


def _seconds_to_utc_midnight() -> float:
    """距离下一个 UTC 午夜还有多少秒。

    免费平台的日额度几乎都按 UTC 自然日重置（OpenRouter、Groq、Cerebras 都是），
    所以文案里点名「每日」时，正确的避让时长是这个数，而不是退避阶梯。
    """
    now = datetime.now(timezone.utc)
    nxt = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
    return (nxt - now).total_seconds()


def _is_daily_quota(msg_l: str) -> bool:
    return any(mk in msg_l for mk in _DAY_MARKERS)


def _is_rate_limited(hdr: dict, msg_l: str) -> bool:
    """这次成功的响应里有没有「其实还在限流」的信号。"""
    for k, v in hdr.items():
        # 头名形如 x-ratelimit-remaining-requests / -tokens，后缀不是 "remaining"
        if k.startswith("x-ratelimit-") and "remaining" in k:
            try:
                if int(re.search(r"-?\d+", v).group()) <= 0:
                    return True
            except (ValueError, AttributeError):
                pass
    return any(mk in msg_l for mk in ("rate limit", "too many requests", "429"))


def _reset_in(bucket: dict, now: float):
    at = (bucket or {}).get("reset_at")
    if at is None:
        return None
    return round(max(0.0, at - now), 1)


# 进程内单例。app.py 与测试都从这里取。
ledger = QuotaLedger()


def scopes_for(provider: str, model: str | None = None) -> list[str]:
    return ledger.scopes_for(provider, model)


def observe(provider: str, model: str | None = None, **kw) -> None:
    ledger.observe(provider, model, **kw)


def note(provider: str, model: str | None = None) -> None:
    ledger.note(provider, model)


def cooldown_left(provider: str, model: str | None = None) -> float:
    return ledger.cooldown_left(provider, model)


def is_cooling(provider: str, model: str | None = None) -> bool:
    return ledger.is_cooling(provider, model)


def force_cooldown(provider: str, model: str | None, seconds: float,
                   reason: str = "") -> None:
    ledger.force_cooldown(provider, model, seconds, reason)


def snapshot(provider: str | None = None) -> list[dict]:
    return ledger.snapshot(provider)


def reset_all() -> None:
    ledger.reset_all()
