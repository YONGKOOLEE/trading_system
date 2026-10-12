"""T-038 L1 위험 계산 변경 (CP-034 D-195, 04 V-267~V-269, A-091 Q-1·A-092).

- 한도 파일에 `l1_entry_buffer_ratio`(여유)가 있으면: 손절가 있는 시장가 매수의 L1 위험 = (신호 기준가 × (1 + 여유) − 손절가)
  × 수량 × |배수|, 지정가 매수 = (주문가 − 손절가), 손절가 없음 = 주문 금액 전체(D-020, 상한가 기준 예약 금액).
  손절가 ≥ 기준가 × (1 + 여유)(지정가는 주문가)면 잘못된 손절가로 G3 거부.
- G8·L2·L3·L7·예약 노출은 상한가 기준 그대로(03 §6.2).
- 여유 키가 없으면 기존 계산(상한가 기준) 그대로 — PAPER 불변은 test_cp034_paper_invariance.

가상 값: 기준 금액 10,000,000원, TST-A 신호 가격 10,000원·상한가 13,000원·하한가 7,000원.
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.scenario.conftest import ACCOUNT, T0, Env
from tests.support.gate_fakes import scenario_limits
from trading_system.domain.enums import Check, GateResult, OrderType, RequestType
from trading_system.domain.market import CashView
from trading_system.domain.models import GateDecision, GateRequest, Signal

BUF = Decimal("0.03")


def _req(qty: str, stop: str | None = None, order_type: OrderType = OrderType.MARKET, price: str = "10000",
         inst: str = "TST-A", ev: str = "e1") -> GateRequest:
    return GateRequest.from_signal(Signal(
        strategy_id="t038", strategy_version=1, evaluation_id=ev, account=ACCOUNT, instrument=inst, rule_id="R1",
        kind=RequestType.ENTRY, quantity=Decimal(qty), order_type=order_type, price=Decimal(price),
        as_of=T0 - timedelta(minutes=1), valid_until=T0 + timedelta(hours=8),
        stop_price=None if stop is None else Decimal(stop)))


def _setup(env: Env, buffer: Decimal | None, **over: object) -> None:
    env.facts.market.add("TST-A", "10000", upper="13000", lower="7000")
    env.facts.limits.by_account[ACCOUNT] = scenario_limits(
        **{"l1_max_loss_ratio": Decimal("0.01"), "l1_entry_buffer_ratio": buffer, **over})   # L1 100,000원


def _snap(env: Env, d: GateDecision) -> dict[str, str]:
    row = env.repo.decision(d.decision_id)
    assert row is not None
    return json.loads(row["limit_snapshot"])


def _ok(d: GateDecision) -> bool:
    return d.result is GateResult.APPROVED


# --- V-267 L1 시장가 매수 ------------------------------------------------------------------------

@pytest.mark.parametrize(("qty", "ok"), [("124", True), ("125", True), ("126", False)])
def test_v267_market_buy_risk_uses_signal_price_plus_buffer(env: Env, qty: str, ok: bool) -> None:
    """(10,000 × 1.03 − 9,500) × 125 = 100,000 = L1 경계 → 경계 −1·경계 통과, +1 거부(G7 L1)."""
    _setup(env, BUF)
    d = env.gate.decide(_req(qty, stop="9500"))
    if ok:
        assert _ok(d), d.reason
        assert Decimal(_snap(env, d)["order_risk"]) == Decimal("800") * Decimal(qty)
        assert Decimal(_snap(env, d)["l1_entry_ref"]) == Decimal("10300")
    else:
        assert d.failed_check is Check.G7 and "L1" in (d.reason or "")


def test_v267_signal_that_upper_limit_basis_would_reject_now_passes(env: Env) -> None:
    """같은 신호: 여유 키 없음(상한가 기준) → (13,000 − 9,500) × 125 = 437,500 > 100,000 거부, 여유 3% → 통과."""
    _setup(env, None)
    d = env.gate.decide(_req("125", stop="9500"))
    assert d.failed_check is Check.G7 and "L1" in (d.reason or "")
    _setup(env, BUF)
    assert _ok(env.gate.decide(_req("125", stop="9500", ev="e2")))


def test_v267_buffer_zero_is_signal_price(env: Env) -> None:
    _setup(env, Decimal("0"), l2_max_symbol_ratio=Decimal("0.30"))            # 상한가 기준 노출 2,600,000 허용
    assert _ok(env.gate.decide(_req("200", stop="9500")))                       # 500 × 200 = 100,000
    assert env.gate.decide(_req("201", stop="9500", inst="TST-B", ev="b")).failed_check is Check.G7


def test_v267_leverage_multiplier_applies(env: Env) -> None:
    _setup(env, BUF)
    env.facts.market.add("TST-L", "10000", leverage="2", upper="13000", lower="7000")
    assert _ok(env.gate.decide(_req("62", stop="9500", inst="TST-L")))          # 800 × 62 × 2 = 99,200
    d = env.gate.decide(_req("63", stop="9500", inst="TST-B", ev="b"))           # 배수 1 종목은 통과 (50,400)
    assert _ok(d)
    env.facts.market.add("TST-M", "10000", leverage="2", upper="13000", lower="7000")
    assert env.gate.decide(_req("63", stop="9500", inst="TST-M", ev="m")).failed_check is Check.G7  # 100,800


# --- V-268 지정가·손절가 없음·잘못된 손절가 ---------------------------------------------------------

@pytest.mark.parametrize(("qty", "ok"), [("199", True), ("200", True), ("201", False)])
def test_v268_limit_buy_risk_is_order_price_minus_stop(env: Env, qty: str, ok: bool) -> None:
    """지정가 10,000원, 손절가 9,500원: (10,000 − 9,500) × 200 = 100,000 경계 (여유 없음 — 체결가 ≤ 주문가)."""
    _setup(env, BUF)
    d = env.gate.decide(_req(qty, stop="9500", order_type=OrderType.LIMIT))
    assert _ok(d) if ok else (d.failed_check is Check.G7 and "L1" in (d.reason or "")), d.reason


@pytest.mark.parametrize(("qty", "ok"), [("7", True), ("8", False)])
def test_v268_no_stop_is_full_notional_at_upper_limit(env: Env, qty: str, ok: bool) -> None:
    """손절가 없음 = 주문 금액 전체(D-020) = 상한가 13,000 × 수량: 7주 91,000 통과, 8주 104,000 거부."""
    _setup(env, BUF)
    d = env.gate.decide(_req(qty))
    assert _ok(d) if ok else (d.failed_check is Check.G7 and "L1" in (d.reason or "")), d.reason
    if ok:
        assert _snap(env, d)["order_risk"] == str(Decimal("13000") * Decimal(qty))


@pytest.mark.parametrize(("stop", "ok"), [("10299", True), ("10300", False), ("10301", False)])
def test_v268_stop_at_or_above_reference_with_buffer_rejected(env: Env, stop: str, ok: bool) -> None:
    """시장가: 손절가 ≥ 기준가 × 1.03(= 10,300) → 잘못된 손절가(G3). 경계 이동(V-121)."""
    _setup(env, BUF)
    d = env.gate.decide(_req("1", stop=stop))
    assert _ok(d) if ok else (d.failed_check is Check.G3 and "손절가" in (d.reason or "")), d.reason


def test_v268_limit_stop_at_or_above_order_price_rejected(env: Env) -> None:
    """지정가는 여유가 없으므로 손절가 ≥ 주문가면 잘못된 손절가(G3)."""
    _setup(env, BUF)
    d = env.gate.decide(_req("1", stop="10000", order_type=OrderType.LIMIT))
    assert d.failed_check is Check.G3 and "손절가" in (d.reason or "")
    assert _ok(env.gate.decide(_req("1", stop="9995", order_type=OrderType.LIMIT, inst="TST-B", ev="b")))


def test_v268_without_buffer_stop_boundary_unchanged(env: Env) -> None:
    """여유 키 없음: 기존 경계(손절가 ≥ 신호 가격 → G3) 그대로."""
    _setup(env, None)
    assert env.gate.decide(_req("1", stop="10000")).failed_check is Check.G3
    assert env.gate.decide(_req("1", stop="9999", inst="TST-B", ev="b")).failed_check is not Check.G3


# --- V-269 현금·노출·L7·예약은 상한가 기준 -----------------------------------------------------------

def test_v269_cash_exposure_reservation_same_as_upper_limit_basis(env: Env) -> None:
    """같은 신호(10주, 손절가 9,500)의 판정 기록 노출·현금·예약 값이 여유 유무와 무관하게 상한가 기준(130,000)."""
    def decide(buffer: Decimal | None, inst: str) -> tuple[dict[str, str], tuple[Decimal, Decimal]]:
        _setup(env, buffer)
        env.facts.market.add(inst, "10000", upper="13000", lower="7000")
        d = env.gate.decide(_req("10", stop="9500", inst=inst, ev=inst))
        assert _ok(d), d.reason
        r = [x for x in env.repo.active_reservations(ACCOUNT) if x.instrument == inst][0]
        return _snap(env, d), (r.amount, r.cash_remaining)

    old_snap, old_rsv = decide(None, "TST-C")
    new_snap, new_rsv = decide(BUF, "TST-D")
    keys = ("notional", "exposure_add", "cash_need", "symbol_limit", "total_limit")
    assert [old_snap[k] for k in keys] == [new_snap[k] for k in keys]
    assert old_snap["notional"] == "130000" and old_rsv == new_rsv == (Decimal("130000"), Decimal("130000"))
    assert (Decimal(old_snap["order_risk"]), Decimal(new_snap["order_risk"])) == (Decimal("35000"), Decimal("8000"))
    assert "l1_entry_ref" not in old_snap        # 여유 없음: 판정 기록 키도 그대로


def test_v269_l7_uses_upper_limit_notional(env: Env) -> None:
    """L7 1,200,000원: 100주 신호 가격 기준 1,000,000원이면 통과겠지만 상한가 기준 1,300,000원 → L7 거부."""
    _setup(env, BUF, l7_max_order_amount=Decimal("1200000"))
    d = env.gate.decide(_req("100", stop="9900"))     # L1 (10,300 − 9,900) × 100 = 40,000 통과
    assert d.failed_check is Check.G7 and "L7" in (d.reason or "")


def test_v269_l2_uses_upper_limit_exposure(env: Env) -> None:
    """L2 1.5%(150,000): 12주 신호 가격 기준 120,000원이지만 상한가 기준 156,000원 → L2 거부."""
    _setup(env, BUF, l2_max_symbol_ratio=Decimal("0.015"))
    d = env.gate.decide(_req("12", stop="9990"))
    assert d.failed_check is Check.G7 and "L2" in (d.reason or "")


def test_v269_cash_uses_upper_limit_notional(env: Env) -> None:
    """주문 가능 현금 1,250,000원: 상한가 기준 1,300,000원 필요 → G8 거부(신호 가격 기준이었으면 통과)."""
    _setup(env, BUF)
    env.facts.account.cashes[ACCOUNT] = CashView(Decimal("1250000"), Decimal("1250000"))
    d = env.gate.decide(_req("100", stop="9990"))
    assert d.failed_check is Check.G8
