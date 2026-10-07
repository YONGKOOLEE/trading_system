"""호출 가중치 예산 (CP-024 D-113, V-145·V-159, BN §4).

rateLimits 형식은 공식 문서 exchangeInfo 예시 형식(값은 합성).
"""

from __future__ import annotations

import pytest

from trading_system.broker.binance.weights import (
    CallPriority as P, RateLimitConfigError, WeightBudget, parse_rate_limits,
)

RATE_LIMITS = [
    {"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 1000},
    {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 20},
    {"rateLimitType": "ORDERS", "interval": "DAY", "intervalNum": 1, "limit": 1000},
    {"rateLimitType": "RAW_REQUESTS", "interval": "MINUTE", "intervalNum": 5, "limit": 300000},
]

T0 = 1_760_000_040_000  # 분 경계에 맞춘 합성 epoch ms (2025-10-09 08:54:00 UTC)


class _Clock:
    def __init__(self, t: int = T0) -> None:
        self.t = t

    def __call__(self) -> int:
        return self.t


def _budget(clock: _Clock | None = None) -> WeightBudget:
    return WeightBudget(parse_rate_limits(RATE_LIMITS), now_ms=clock or _Clock())


def test_parse_rate_limits() -> None:
    rl = parse_rate_limits(RATE_LIMITS)
    assert [(w.interval_ms, w.limit) for w in rl.weight] == [(60_000, 1000)]
    assert sorted((o.interval_ms, o.limit) for o in rl.orders) == [(10_000, 20), (86_400_000, 1000)]


@pytest.mark.parametrize("bad", [
    [], None, [{"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 20}],
    [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 0}],
    [{"rateLimitType": "REQUEST_WEIGHT", "interval": "FORTNIGHT", "intervalNum": 1, "limit": 10}],
    [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": "1", "limit": 10}],
])
def test_parse_rate_limits_refuses_unknown_or_missing(bad: object) -> None:
    # 못 읽으면 시작 거부 (D-113)
    with pytest.raises(RateLimitConfigError):
        parse_rate_limits(bad)


def test_internal_budget_is_half_of_limit() -> None:
    b = _budget()
    assert b.weight_budget(60_000) == 500
    assert b.admit(500, P.RESOLVE).ok
    assert not b.admit(1, P.RESOLVE).ok


def test_priority_tiers_hold_back_risk_increase_and_market_first() -> None:
    # V-159: 예산이 줄면 시세 → 위험 확대 → 대조 순으로 보류. 결과 불명 조회·위험 축소는 마지막까지
    b = _budget()
    assert b.admit(340, P.RESOLVE).ok         # 사용 340 / 예산 500
    assert not b.admit(20, P.MARKET).ok       # 시세 상한 70% = 350
    assert b.admit(20, P.INCREASE).ok          # 360 ≤ 80% = 400
    assert not b.admit(50, P.INCREASE).ok
    assert b.admit(80, P.RECONCILE).ok         # 440 ≤ 90% = 450
    assert not b.admit(20, P.RECONCILE).ok
    assert b.admit(30, P.REDUCE).ok            # 470 ≤ 500
    assert b.admit(30, P.RESOLVE).ok           # 500
    denied = b.admit(1, P.REDUCE)
    assert not denied.ok and "예산" in denied.reason


def test_header_usage_corrects_local_estimate() -> None:
    b = _budget()
    b.admit(10, P.MARKET)
    b.observe_headers({"x-mbx-used-weight-1m": "420", "Content-Type": "application/json"})
    assert b.used_weight(60_000) == 420
    assert not b.admit(20, P.INCREASE).ok
    # 헤더가 지역 추정보다 작으면 큰 쪽(보수)을 쓴다
    b.admit(5, P.RESOLVE)
    b.observe_headers({"X-MBX-USED-WEIGHT-1M": "3"})
    assert b.used_weight(60_000) == 425


def test_window_rolls_over_at_interval_boundary() -> None:
    clk = _Clock()
    b = _budget(clk)
    b.admit(500, P.RESOLVE)
    clk.t += 60_000
    assert b.used_weight(60_000) == 0
    assert b.admit(10, P.MARKET).ok


def test_order_count_budget() -> None:
    b = _budget()
    for _ in range(8):                       # ORDERS 10초 20건 → 예산 10, 위험 확대는 80%까지
        assert b.admit_order(P.INCREASE).ok
    assert not b.admit_order(P.INCREASE).ok
    assert b.admit_order(P.REDUCE).ok
    assert b.admit_order(P.REDUCE).ok
    assert not b.admit_order(P.REDUCE).ok
    b.observe_headers({"X-MBX-ORDER-COUNT-10S": "3", "X-MBX-ORDER-COUNT-1D": "3"})
    assert b.order_count(10_000) == 10       # 큰 쪽 유지


def test_429_cooldown_blocks_until_retry_after() -> None:
    clk = _Clock()
    b = _budget(clk)
    b.on_429(retry_after_s=7)
    adm = b.admit(1, P.RESOLVE)
    assert not adm.ok and adm.wait_s == pytest.approx(7)
    assert not b.admit_order(P.REDUCE).ok
    clk.t += 7_000
    assert b.admit(1, P.RESOLVE).ok


def test_429_without_retry_after_uses_conservative_default() -> None:
    clk = _Clock()
    b = _budget(clk)
    b.on_429(retry_after_s=None)
    assert not b.admit(1, P.RESOLVE).ok
    clk.t += 60_000
    assert b.admit(1, P.RESOLVE).ok


def test_418_stops_everything_until_human_resume() -> None:
    clk = _Clock()
    b = _budget(clk)
    b.on_418()
    assert b.banned
    clk.t += 3 * 86_400_000          # 시간이 지나도 자동 재개 없음 (D-113 M2 예외)
    adm = b.admit(1, P.RESOLVE)
    assert not adm.ok and "418" in adm.reason
    assert not b.admit_order(P.REDUCE).ok
    b.resume_after_ban()
    assert not b.banned and b.admit(1, P.RESOLVE).ok


def test_invalid_weight_rejected() -> None:
    with pytest.raises(ValueError):
        _budget().admit(-1, P.RESOLVE)
