"""T-003 게이트 완성 (05-plan T-003, 04 §3.1 V-01~V-13, 01 §2.2~§2.5·§2.8).

기준 금액 10,000,000원, 가상 종목 10,000원(상한가 = 현재가로 둔 가상값), 한도는 gate_fakes.scenario_limits.
모든 테스트 종료 시 conftest가 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from tests.scenario.conftest import ACCOUNT, T0, Env
from tests.support.gate_fakes import TEST_POLICY, scenario_limits
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.versioning import ChangeSource, SettingsManager
from trading_system.domain.enums import (
    Check, GateResult, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.market import (
    CashView, CostRates, DailyRisk, InstrumentKind, SessionInfo, TradingStatus, estimate_cost,
)
from trading_system.domain.models import GateDecision, GateRequest, Signal
from trading_system.gate.gate import Gate
from trading_system.gate.ports import GateInputs


def sig(kind: RequestType, inst: str = "TST-A", qty: str = "100", ev: str = "e1",
        order_type: OrderType = OrderType.MARKET, price: str = "10000", strategy: str = "t001-fixed",
        at: datetime = T0) -> GateRequest:
    s = Signal(strategy_id=strategy, strategy_version=1, evaluation_id=ev, account=ACCOUNT, instrument=inst,
               rule_id="R1", kind=kind, quantity=Decimal(qty), order_type=order_type, price=Decimal(price),
               as_of=at - timedelta(minutes=1), valid_until=at + timedelta(hours=8))
    return GateRequest.from_signal(s)


def op(kind: RequestType, inst: str = "TST-A", qty: str = "100", key: str = "op-1",
       side: Side = Side.SELL, order_type: OrderType = OrderType.MARKET, price: str = "10000") -> GateRequest:
    return GateRequest(source=RequestSource.OPERATOR, request_type=kind, account=ACCOUNT, instrument=inst, side=side,
                       quantity=Decimal(qty), order_type=order_type, price=Decimal(price), idem_key=key)


def run(env: Env, req: GateRequest) -> GateDecision:
    d = env.gate.decide(req)
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


def hold(env: Env, inst: str = "TST-A", qty: str = "100", ev: str = "h") -> None:
    d = run(env, sig(RequestType.ENTRY, inst, qty, ev=f"{ev}-{inst}"))
    assert d.result is GateResult.APPROVED, d.reason
    assert env.repo.position(ACCOUNT, inst).quantity == Decimal(qty)


def rejected(d: GateDecision, check: Check, text: str | None = None) -> bool:
    ok = d.result is GateResult.REJECTED and d.failed_check is check
    return ok and (text is None or text in (d.reason or ""))


def set_limits(env: Env, **over: object) -> None:
    env.facts.limits.by_account[ACCOUNT] = scenario_limits(**over)


def set_time(env: Env, hh: int, mm: int, ss: int = 0) -> None:
    kst = datetime(2026, 10, 1, hh, mm, ss, tzinfo=UTC) - timedelta(hours=9)
    env.clock._now = kst  # 테스트 시계 이동 (FixedClock 내부 값)


# --- V-01 한도별 경계 (경계 −1·경계·+1, 초과만 거부. 사유는 해당 검사 번호) ---------------

@pytest.mark.parametrize(("qty", "ok"), [("49", True), ("50", True), ("51", False)])
def test_v01_l1_max_loss_boundary(env: Env, qty: str, ok: bool) -> None:
    set_limits(env, l1_max_loss_ratio=Decimal("0.05"))  # 500,000원 (손절 규칙 없음 → 주문 금액 전체, D-020)
    d = run(env, sig(RequestType.ENTRY, qty=qty))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G7, "L1")


@pytest.mark.parametrize(("pnl", "ok"), [("-199999", True), ("-200000", False), ("-200001", False)])
def test_v01_l4_daily_loss_boundary(env: Env, pnl: str, ok: bool) -> None:
    """L4 2% = 200,000원. 01 §2.3 "도달 시 정지"에 따라 경계(도달)도 거부한다 (V-01과의 차이는 CP-017 제안)."""
    env.facts.account.daily[ACCOUNT] = DailyRisk(Decimal("10000000"), Decimal(pnl), Decimal("0"), Decimal("0"))
    d = run(env, sig(RequestType.ENTRY))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G7, "L4")


@pytest.mark.parametrize(("limit", "ok"), [(3, True), (2, False)])
def test_v01_l5_daily_new_orders(env: Env, limit: int, ok: bool) -> None:
    set_limits(env, l5_daily_new_orders=limit)
    hold(env, "TST-A", "10")
    hold(env, "TST-B", "10")
    d = run(env, sig(RequestType.ENTRY, "TST-C", "10", ev="third"))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G7, "L5")


def test_v01_l5_counts_unknown_but_not_broker_rejected(env: Env) -> None:
    set_limits(env, l5_daily_new_orders=2)
    env.broker.behavior = FakeBehavior.REJECT
    run(env, sig(RequestType.ENTRY, "TST-A", "10", ev="r"))          # 브로커 거부: 세지 않음
    env.broker.behavior = FakeBehavior.UNKNOWN
    run(env, sig(RequestType.ENTRY, "TST-B", "10", ev="u"))          # 결과 불명: 셈
    env.broker.behavior = FakeBehavior.FILL
    assert run(env, sig(RequestType.ENTRY, "TST-C", "10", ev="c")).result is GateResult.APPROVED  # 2번째
    env.facts.market.add("TST-D", "10000", upper="10000")
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-D", "10", ev="d")), Check.G7, "L5")


def test_v01_l5_resets_at_daily_reference_time(env: Env) -> None:
    set_limits(env, l5_daily_new_orders=1)
    hold(env, "TST-A", "10")
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-B", "10", ev="b1")), Check.G7, "L5")
    env.clock.advance(timedelta(days=1))  # 다음 날 09:05 KST (기준 시각 08:00 지남)
    assert run(env, sig(RequestType.ENTRY, "TST-B", "10", ev="b2", at=env.clock.now())).result is GateResult.APPROVED


@pytest.mark.parametrize(("limit", "ok"), [(3, True), (2, False)])
def test_v01_l6_max_positions(env: Env, limit: int, ok: bool) -> None:
    set_limits(env, l6_max_positions=limit)
    hold(env, "TST-A", "10")
    hold(env, "TST-B", "10")
    d = run(env, sig(RequestType.ENTRY, "TST-C", "10", ev="third"))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G7, "L6")
    # 증액은 보유 종목 수를 늘리지 않으므로 L6 대상이 아님
    assert run(env, sig(RequestType.INCREASE, "TST-A", "10", ev="inc")).result is GateResult.APPROVED


def test_v01_l6_counts_pending_entries(env: Env) -> None:
    set_limits(env, l6_max_positions=1)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert run(env, sig(RequestType.ENTRY, "TST-A", "10")).result is GateResult.APPROVED  # 미체결 진입 예약
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-B", "10", ev="b")), Check.G7, "L6")


@pytest.mark.parametrize(("qty", "ok"), [("99", True), ("100", True), ("101", False)])
def test_v01_l7_max_order_amount(env: Env, qty: str, ok: bool) -> None:
    set_limits(env, l7_max_order_amount=Decimal("1000000"))
    d = run(env, sig(RequestType.ENTRY, qty=qty))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G7, "L7")


@pytest.mark.parametrize(("hms", "ok"), [((9, 4, 59), False), ((9, 5, 0), True), ((15, 14, 59), True),
                                         ((15, 15, 0), False)])
def test_v01_l8_order_window_is_g12(env: Env, hms: tuple[int, int, int], ok: bool) -> None:
    set_time(env, *hms)
    d = run(env, sig(RequestType.ENTRY))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G12, "L8")


@pytest.mark.parametrize(("qty", "ok"), [("49", True), ("50", True), ("51", False)])
def test_v01_l10_leveraged_total(env: Env, qty: str, ok: bool) -> None:
    set_limits(env, l10_leverage_total_ratio=Decimal("0.10"))  # 1,000,000원, 2배 상품은 주당 노출 20,000원
    env.facts.market.add("TST-L", "10000", leverage="2", kind=InstrumentKind.ETF, upper="10000")
    d = run(env, sig(RequestType.ENTRY, "TST-L", qty))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G7, "L10")


# --- V-02 레버리지 배수 노출 -------------------------------------------------------

@pytest.mark.parametrize("lev", ["2", "-2"])
def test_v02_leverage_exposure_is_value_times_abs_multiplier(env: Env, lev: str) -> None:
    env.facts.market.add("TST-L", "10000", leverage=lev, kind=InstrumentKind.ETF, upper="10000")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, "TST-L", "100"))
    assert d.result is GateResult.APPROVED
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert rsv.amount == Decimal("2000000")  # 1,000,000원 × |배수| 2
    assert rsv.cash_amount == Decimal("1000000")  # 현금은 배수와 무관


def test_v02_held_leveraged_position_counts_multiplied(env: Env) -> None:
    env.facts.market.add("TST-L", "10000", leverage="2", kind=InstrumentKind.ETF, upper="10000")
    hold(env, "TST-L", "100")  # 노출 2,000,000원 = L2 경계
    assert env.repo.active_reservations(ACCOUNT) == []
    assert rejected(run(env, sig(RequestType.INCREASE, "TST-L", "1", ev="inc")), Check.G7, "L2")


# --- V-03 손실률·비용 -------------------------------------------------------------

def test_v03_loss_ratio_definition() -> None:
    d = DailyRisk(Decimal("10000000"), realized=Decimal("-100000"), unrealized_change=Decimal("-50000"),
                  costs=Decimal("10000"))
    assert d.pnl == Decimal("-160000") and d.loss_ratio == Decimal("0.016")
    gain = DailyRisk(Decimal("10000000"), Decimal("50000"), Decimal("0"), Decimal("10000"))
    assert gain.loss_ratio == 0
    # 이익이 비용보다 작으면 손실
    assert DailyRisk(Decimal("1000"), Decimal("5"), Decimal("0"), Decimal("15")).loss_ratio == Decimal("0.01")


def test_v03_cost_estimate_rounds_up_to_won() -> None:
    rates = CostRates(commission_rate=Decimal("0.00015"), sell_tax_rate=Decimal("0.0018"))  # 가상 요율
    assert estimate_cost(Decimal("1000000"), False, rates) == Decimal("150")
    assert estimate_cost(Decimal("1000001"), False, rates) == Decimal("151")  # 150.00015 → 151
    assert estimate_cost(Decimal("1000000"), True, rates) == Decimal("1950")


def test_v03_unevaluable_daily_risk_rejects_risk_increase(env: Env) -> None:
    env.facts.account.daily[ACCOUNT] = DailyRisk(Decimal("10000000"), Decimal("0"), Decimal("0"), Decimal("0"),
                                                 evaluable=False)
    assert rejected(run(env, sig(RequestType.ENTRY)), Check.G7, "평가 불능")
    env.facts.account.daily[ACCOUNT] = None
    assert rejected(run(env, sig(RequestType.ENTRY, ev="e2")), Check.G7, "일일 리스크")


# --- V-05 보수적 계산 (미체결 매수·결과 불명 매수는 전량 체결로 가정) -----------------

def test_v05_open_and_unknown_buys_count_fully(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert run(env, sig(RequestType.ENTRY, "TST-A", "200", ev="a")).result is GateResult.APPROVED  # 미체결 2,000,000
    env.broker.behavior = FakeBehavior.UNKNOWN
    assert run(env, sig(RequestType.ENTRY, "TST-B", "200", ev="b")).result is GateResult.APPROVED  # 결과 불명 2,000,000
    env.broker.behavior = FakeBehavior.FILL
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-C", "101", ev="c")), Check.G7, "L3")  # 4,000,000 + 1,010,000
    assert run(env, sig(RequestType.ENTRY, "TST-C", "100", ev="c2")).result is GateResult.APPROVED


def test_v05_market_order_reserves_at_upper_limit(env: Env) -> None:
    env.facts.market.upper["TST-A"] = Decimal("13000")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    run(env, sig(RequestType.ENTRY, "TST-A", "100"))
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert rsv.amount == Decimal("1300000")


def test_v05_market_order_without_upper_limit_uses_price_limit_ratio(env: Env) -> None:
    env.facts.market.upper["TST-A"] = None
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    run(env, sig(RequestType.ENTRY, "TST-A", "100"))
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert rsv.amount == Decimal("1300000")  # 10,000 × (1 + 0.30) × 100


def test_v05_limit_order_reserves_at_order_price(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    run(env, sig(RequestType.ENTRY, "TST-A", "100", order_type=OrderType.LIMIT, price="9500"))
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert rsv.amount == Decimal("950000")


# --- V-06 위험 축소는 한도 비적용 ---------------------------------------------------

def test_v06_exit_allowed_when_over_limits(env: Env) -> None:
    hold(env, "TST-A", "200")
    set_limits(env, l1_max_loss_ratio=Decimal("0.01"), l2_max_symbol_ratio=Decimal("0.01"),
               l3_max_total_ratio=Decimal("0.01"), l7_max_order_amount=Decimal("1"))
    env.facts.account.daily[ACCOUNT] = DailyRisk(Decimal("10000000"), Decimal("-900000"), Decimal("0"), Decimal("0"))
    env.facts.account.cashes[ACCOUNT] = CashView(Decimal("0"), Decimal("0"))
    assert rejected(run(env, sig(RequestType.INCREASE, "TST-A", "1", ev="inc")), Check.G7)
    assert run(env, sig(RequestType.REDUCE, "TST-A", "50", ev="red")).result is GateResult.APPROVED
    assert run(env, sig(RequestType.EXIT, "TST-A", "150", ev="ex")).result is GateResult.APPROVED
    assert env.repo.position(ACCOUNT, "TST-A").quantity == 0


# --- V-08 주문 가능 현금 ------------------------------------------------------------

def test_v08_cash_shortage_rejected_by_g8(env: Env) -> None:
    env.facts.account.cashes[ACCOUNT] = CashView(Decimal("10000000"), Decimal("999999"))  # 브로커 값이 더 작음
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-A", "100")), Check.G8)
    assert run(env, sig(RequestType.ENTRY, "TST-A", "99", ev="e2")).result is GateResult.APPROVED


def test_v08_internal_cash_subtracts_active_buy_reservations(env: Env) -> None:
    env.facts.account.cashes[ACCOUNT] = CashView(Decimal("1500000"), Decimal("10000000"))
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert run(env, sig(RequestType.ENTRY, "TST-A", "100")).result is GateResult.APPROVED  # 1,000,000 예약
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-B", "51", ev="b")), Check.G8)      # 510,000 > 500,000
    assert run(env, sig(RequestType.ENTRY, "TST-B", "50", ev="b2")).result is GateResult.APPROVED


def test_v08_cost_estimate_included(env: Env) -> None:
    gate = Gate(env.repo, env.clock, env.facts.inputs(),
                type(TEST_POLICY)(TEST_POLICY.quote_max_age, TEST_POLICY.price_limit_ratio,
                                  CostRates(Decimal("0.001"), Decimal("0"))))
    env.facts.account.cashes[ACCOUNT] = CashView(Decimal("1000000"), Decimal("1000000"))
    d = gate.decide(sig(RequestType.ENTRY, "TST-A", "100"))  # 1,000,000 + 비용 1,000
    assert rejected(d, Check.G8)


def test_v08_unknown_cash_rejects(env: Env) -> None:
    env.facts.account.cashes[ACCOUNT] = None
    assert rejected(run(env, sig(RequestType.ENTRY)), Check.G8)


# --- V-09 매도 수량 -----------------------------------------------------------------

def test_v09_sell_beyond_position_minus_active_sells(env: Env) -> None:
    hold(env, "TST-A", "100")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    assert run(env, sig(RequestType.REDUCE, "TST-A", "60", ev="r1")).result is GateResult.APPROVED
    d = env.gate.decide(op(RequestType.REDUCE, "TST-A", "41", key="op-r"))
    assert d.result is GateResult.REJECTED  # 같은 방향 처리 중 의도(G4)가 먼저 막고, 수량도 초과
    assert (Check.G9, False) in d.checks


# --- V-10 가격·수량·호가 단위 --------------------------------------------------------

@pytest.mark.parametrize(("price", "ok"), [("9995", True), ("9997", False)])
def test_v10_tick_size(env: Env, price: str, ok: bool) -> None:
    d = run(env, sig(RequestType.ENTRY, order_type=OrderType.LIMIT, price=price))
    assert (d.result is GateResult.APPROVED) if ok else rejected(d, Check.G10, "호가")


def test_v10_limit_price_outside_price_band(env: Env) -> None:
    env.facts.market.upper["TST-A"] = Decimal("13000")
    env.facts.market.lower["TST-A"] = Decimal("7000")
    assert rejected(run(env, sig(RequestType.ENTRY, order_type=OrderType.LIMIT, price="13005")), Check.G10)
    hold(env, "TST-B", "10")
    env.facts.market.lower["TST-B"] = Decimal("7000")
    assert rejected(run(env, sig(RequestType.EXIT, "TST-B", "10", ev="x", order_type=OrderType.LIMIT,
                                 price="6995")), Check.G10)


def test_v10_unknown_tick_rule_rejects_limit_orders(env: Env) -> None:
    env.facts.market.tick = None
    assert rejected(run(env, sig(RequestType.ENTRY, order_type=OrderType.LIMIT, price="10000")), Check.G10)
    assert run(env, sig(RequestType.ENTRY, ev="m")).result is GateResult.APPROVED  # 시장가는 호가 무관


def test_v10_lot_size(env: Env) -> None:
    from trading_system.domain.market import InstrumentInfo
    env.facts.market.infos["TST-A"] = InstrumentInfo("TST-A", InstrumentKind.STOCK, Decimal("1"), Decimal("10"))
    assert rejected(run(env, sig(RequestType.ENTRY, qty="15")), Check.G10, "수량 단위")


def test_v10_unknown_instrument_rejects_risk_reduction_too(env: Env) -> None:
    hold(env, "TST-A", "10")
    del env.facts.market.infos["TST-A"]
    assert rejected(run(env, sig(RequestType.EXIT, "TST-A", "10", ev="x")), Check.G10, "종목 정보")


# --- V-11 거래 시간·달력 --------------------------------------------------------------

def test_v11_holiday_rejects_everything(env: Env) -> None:
    hold(env, "TST-A", "10")
    env.facts.market.sessions[T0.date()] = SessionInfo(False)
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-B", ev="b")), Check.G12, "휴장")
    assert rejected(run(env, sig(RequestType.EXIT, "TST-A", "10", ev="x")), Check.G12, "휴장")


def test_v11_unknown_calendar_rejects(env: Env) -> None:
    env.facts.market.sessions[T0.date()] = None
    assert rejected(run(env, sig(RequestType.ENTRY)), Check.G12, "달력")


def test_v11_risk_reduction_allowed_in_regular_session_outside_order_window(env: Env) -> None:
    hold(env, "TST-A", "10")
    set_time(env, 15, 20)  # 주문 허용 시간대(~15:15) 밖, 정규장(~15:30) 안
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-B", ev="b")), Check.G12, "L8")
    assert run(env, sig(RequestType.EXIT, "TST-A", "10", ev="x")).result is GateResult.APPROVED


def test_v11_outside_regular_session_rejects_risk_reduction(env: Env) -> None:
    hold(env, "TST-A", "10")
    set_time(env, 15, 30)
    assert rejected(run(env, sig(RequestType.EXIT, "TST-A", "10", ev="x")), Check.G12, "정규장")


# --- V-13 운영 요청으로 신규 진입 불가 (R-21) ---------------------------------------------

@pytest.mark.parametrize("kind", [RequestType.ENTRY, RequestType.INCREASE])
def test_v13_operator_cannot_increase_risk(env: Env, kind: RequestType) -> None:
    hold(env, "TST-A", "10")
    d = run(env, op(kind, "TST-A", "10", side=Side.BUY))
    assert rejected(d, Check.G3)
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("10")


def test_v13_operator_can_exit_any_owner(env: Env) -> None:
    hold(env, "TST-A", "10")
    assert run(env, op(RequestType.EXIT, "TST-A", "10")).result is GateResult.APPROVED


# --- G5 결과 불명 --------------------------------------------------------------------

def test_g5_unknown_sell_blocks_buy_and_sell_on_same_symbol(env: Env) -> None:
    hold(env, "TST-A", "100")
    env.broker.behavior = FakeBehavior.UNKNOWN
    run(env, sig(RequestType.REDUCE, "TST-A", "10", ev="r"))
    env.broker.behavior = FakeBehavior.FILL
    assert rejected(run(env, sig(RequestType.INCREASE, "TST-A", "10", ev="inc")), Check.G5)
    assert run(env, sig(RequestType.ENTRY, "TST-B", "10", ev="b")).result is GateResult.APPROVED  # 다른 종목 허용
    d = env.gate.decide(op(RequestType.EXIT, "TST-A", "90", key="op-x"))
    assert d.result is GateResult.REJECTED and (Check.G5, False) in d.checks


# --- G6 전략 상태 ---------------------------------------------------------------------

def test_g6_strategy_state_blocks_signals_not_operator(env: Env) -> None:
    hold(env, "TST-A", "10")
    env.facts.strategies.blocked["t001-fixed:1"] = "중지(사용자 판단)"
    assert rejected(run(env, sig(RequestType.EXIT, "TST-A", "10", ev="x")), Check.G6, "전략 상태")
    assert run(env, op(RequestType.EXIT, "TST-A", "10")).result is GateResult.APPROVED


# --- G11 시세 신선도 (Q-C) ---------------------------------------------------------------

def test_g11_stale_quote(env: Env) -> None:
    hold(env, "TST-A", "10")
    env.facts.market.quote_age["TST-A"] = timedelta(seconds=61)
    assert rejected(run(env, sig(RequestType.INCREASE, "TST-A", "10", ev="i")), Check.G11)
    assert rejected(run(env, sig(RequestType.REDUCE, "TST-A", "5", ev="r")), Check.G11)  # 시장가 감액 거부
    d = run(env, sig(RequestType.REDUCE, "TST-A", "5", ev="r2", order_type=OrderType.LIMIT, price="10000"))
    assert d.result is GateResult.APPROVED  # 지정가는 허용


def test_g11_stale_held_position_blocks_other_entries(env: Env) -> None:
    """보유 평가 가격이 신선하지 않으면 평가 불능 → 위험 확대 거부 (01 §2.4)."""
    hold(env, "TST-A", "10")
    env.facts.market.quote_age["TST-A"] = timedelta(minutes=5)
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-B", "10", ev="b")), Check.G11)


def test_g11_quote_from_future_rejected(env: Env) -> None:
    env.facts.market.quote_age["TST-A"] = timedelta(minutes=-5)
    assert rejected(run(env, sig(RequestType.ENTRY)), Check.G11)


# --- G13 종목 거래 상태 (01 §2.8) ------------------------------------------------------

@pytest.mark.parametrize(("status", "exit_ok"), [
    (TradingStatus.HALTED, False), (TradingStatus.VI, False), (TradingStatus.UPPER_LIMIT, True),
    (TradingStatus.LOWER_LIMIT, True), (TradingStatus.CAUTION, True), (TradingStatus.DELISTING, True),
    (TradingStatus.ETN_REDEMPTION, True),
])
def test_g13_trading_status_table(env: Env, status: TradingStatus, exit_ok: bool) -> None:
    hold(env, "TST-A", "10")
    env.facts.market.statuses["TST-A"] = frozenset({status})
    assert rejected(run(env, sig(RequestType.INCREASE, "TST-A", "1", ev="i")), Check.G13)
    d = run(env, sig(RequestType.EXIT, "TST-A", "10", ev="x"))
    assert (d.result is GateResult.APPROVED) if exit_ok else rejected(d, Check.G13)


def test_g13_unknown_status_blocks_entry_only(env: Env) -> None:
    hold(env, "TST-A", "10")
    env.facts.market.statuses["TST-A"] = None
    assert rejected(run(env, sig(RequestType.INCREASE, "TST-A", "1", ev="i")), Check.G13)
    assert run(env, sig(RequestType.EXIT, "TST-A", "10", ev="x")).result is GateResult.APPROVED


def test_g13_cancel_allowed_during_halt_status(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, "TST-A", "10"))
    order_key = env.repo.intent(d.approved.intent_id).broker_order_key
    env.facts.market.statuses["TST-A"] = frozenset({TradingStatus.HALTED})
    c = env.gate.decide(GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, "TST-A", Side.BUY, order_key,
                                           idem_key="op-c", owner=None))
    assert c.result is GateResult.APPROVED


# --- G14 시계 오차 (K6 Q-H) --------------------------------------------------------------

@pytest.mark.parametrize("ok", [False, None])
def test_g14_clock_problem(env: Env, ok: bool | None) -> None:
    hold(env, "TST-A", "20")
    env.facts.clock_health.ok = ok
    assert rejected(run(env, sig(RequestType.ENTRY, "TST-B", "10", ev="b")), Check.G14)
    assert rejected(run(env, sig(RequestType.REDUCE, "TST-A", "10", ev="r")), Check.G14)  # 전략 신호 거부
    assert run(env, op(RequestType.REDUCE, "TST-A", "10")).result is GateResult.APPROVED  # 사람의 위험 축소 허용


# --- 정정 (01 §2.1·§2.5) -------------------------------------------------------------------

def _open_buy(env: Env, qty: str = "100", price: str = "10000") -> str:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(RequestType.ENTRY, "TST-A", qty, order_type=OrderType.LIMIT, price=price))
    return env.repo.intent(d.approved.intent_id).broker_order_key


def _modify(order_key: str, qty: str, price: str, source: RequestSource = RequestSource.EXEC_POLICY,
            key: str = "m-1", side: Side = Side.BUY) -> GateRequest:
    return GateRequest(source=source, request_type=RequestType.MODIFY, account=ACCOUNT, instrument="TST-A",
                       side=side, quantity=Decimal(qty), order_type=OrderType.LIMIT, price=Decimal(price),
                       idem_key=key, owner="t001-fixed:1", target_order_key=order_key)


def test_modify_quantity_increase_rejected(env: Env) -> None:
    key = _open_buy(env)
    assert rejected(env.gate.decide(_modify(key, "101", "10000")), Check.G10, "수량 증가")


def test_modify_only_from_exec_policy(env: Env) -> None:
    key = _open_buy(env)
    assert rejected(env.gate.decide(_modify(key, "100", "9995", source=RequestSource.OPERATOR)), Check.G3)


def test_modify_buy_rechecks_limits_with_worst_case(env: Env) -> None:
    env.facts.market.upper["TST-A"] = Decimal("15000")
    key = _open_buy(env, "150", "10000")  # 예약 1,500,000
    # 정정 후 주문 2,025,000 > 2,000,000 (이 가상 한도는 L1 = L2라 L1이 먼저 걸린다)
    assert rejected(env.gate.decide(_modify(key, "150", "13500", key="m-up")), Check.G7, "L1")
    d = env.gate.decide(_modify(key, "150", "13000", key="m-ok"))  # 1,950,000
    assert d.result is GateResult.APPROVED
    added = env.repo.reservation_for(d.approved.intent_id)
    assert added is not None and added.amount == Decimal("450000")  # 원주문보다 커지는 만큼만 추가 예약


def test_modify_side_must_match_target(env: Env) -> None:
    key = _open_buy(env)
    assert rejected(env.gate.decide(_modify(key, "100", "9995", side=Side.SELL)), Check.G4)


# --- 판정 기록 (ENT-14) -------------------------------------------------------------------

def test_decision_records_settings_version_snapshot_and_diagnostics(env: Env) -> None:
    env.facts.clock_health.ok = False
    d = run(env, sig(RequestType.ENTRY, qty="300"))  # G7(L2)와 G14 둘 다 실패
    assert rejected(d, Check.G7)
    assert (Check.G14, False) in d.checks and (Check.G12, True) in d.checks
    row = env.repo.decision(d.decision_id)
    assert row["settings_hash"] == scenario_limits().version_hash()
    snap = json.loads(row["limit_snapshot"])
    assert snap["base_amount"] == "10000000" and snap["symbol_after"] == "3000000"
    assert [c for c, _ in json.loads(row["checks"])] == [f"G{i}" for i in range(1, 15)]


def test_gate_uses_settings_manager_effective_limits(env: Env) -> None:
    """게이트를 SettingsManager(T-002)에 연결: 설정 없으면 거부, 등록하면 그 값으로 판정."""
    settings = SettingsManager(env.repo, env.clock)
    inputs = GateInputs(settings, env.facts.market, env.facts.account, env.facts.strategies, env.facts.clock_health)
    gate = Gate(env.repo, env.clock, inputs, TEST_POLICY)
    assert rejected(gate.decide(sig(RequestType.ENTRY, ev="s1")), Check.G7, "한도 설정 없음")
    settings.submit_account_limits(ACCOUNT, scenario_limits(l2_max_symbol_ratio=Decimal("0.05")), ChangeSource.LOCAL)
    assert rejected(gate.decide(sig(RequestType.ENTRY, ev="s2", qty="51")), Check.G7, "L2")
    assert gate.decide(sig(RequestType.ENTRY, ev="s3", qty="50")).result is GateResult.APPROVED

