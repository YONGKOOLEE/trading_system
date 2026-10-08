"""T-M2-07 시장별 현금 정책·일일 리스크 분리 (CP-024 §2.4·§2.5, D-108, D-118).

- V-151: 매수 체결 즉시 USDT 감소, T+2 보수 차감(이중 차감) 없음. 현금 부족 매수는 G8 거부(전송 0).
  주문 가능 USDT = min(거래소 USDT free, 내부 기대 USDT − 활성 매수 예약 − 추정 수수료(D-118 올림)).
- V-140(L4·기준 금액 부분): M2 기준 금액 = 일 경계(UTC 00:00) 시점 USDT free+locked + 보유 평가액,
  L4 일일 손실은 UTC 00:00 경계로 나뉘고 KST 자정과 무관.
- 일일 리스크 상태 시장별 분리: M2는 SpotAccountFacts(UTC·USDT), M1은 기존 DailyRiskTracker·ConservativeCashFacts 그대로.
모든 값은 가상이다(tests/support/m2_fakes.py). 네트워크 없음. 매 시험 끝에 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import (
    ACCOUNT, CASH_MAX_AGE, FEE_RATE, M2_POLICY, QUOTE_ASSET, RULES, T0, M2Env, asset_pair, build_m2_env, ioc_buy_price,
)
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import Check, GateResult, Mode, OrderType, RequestType
from trading_system.domain.market import CostRates, estimate_cost
from trading_system.domain.market_time import M2_TIME
from trading_system.domain.models import GateRequest, Signal
from trading_system.monitoring.spot_cash import QuoteBalance, SpotAccountFacts
from trading_system.storage.repository import Repository

STRATEGY = "tm207-fixed"  # 테스트 전용 고정 전략 (실제 매매전략 아님)
MAX_AGE = CASH_MAX_AGE  # 거래소 잔고 조회 신선도 (시험 가상값)
DAY_START = datetime(2026, 10, 7, 0, 0, tzinfo=UTC)  # UTC 00:00 = KST 09:00


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path, spot_cash=True)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def facts(env: M2Env) -> SpotAccountFacts:
    assert isinstance(env.account, SpotAccountFacts)
    return env.account


def signal(env: M2Env, kind: RequestType, instrument: str = "TSTA-USDT", quantity: str = "0.1000",
           price: str | None = None, evaluation: str = "eval-1") -> Signal:
    """매수는 IOC 지정가 가격 규칙(D-104), 매도는 시장가가 기본. 신호 시각은 시험 시계 기준."""
    rule = RULES[instrument]
    buying = kind in (RequestType.ENTRY, RequestType.INCREASE)
    otype = OrderType.LIMIT if buying else OrderType.MARKET
    px = Decimal(price) if price is not None else (ioc_buy_price(rule.last, rule.tick_size) if buying else rule.last)
    now = env.clock.now()
    return Signal(strategy_id=STRATEGY, strategy_version=1, evaluation_id=evaluation, account=ACCOUNT,
                  instrument=instrument, rule_id="R1", kind=kind, quantity=Decimal(quantity), order_type=otype,
                  price=px, as_of=now - timedelta(minutes=1), valid_until=now + timedelta(minutes=30))


def run(env: M2Env, sig: Signal):  # type: ignore[no-untyped-def]
    decision = env.gate.decide(GateRequest.from_signal(sig))
    if decision.approved is not None:
        env.executor.submit(decision.approved)
        env.executor.process_events()
    return decision


def at(env: M2Env, when: datetime) -> None:
    env.clock.advance(when - env.clock.now())


def balance(env: M2Env, free: str, locked: str = "0") -> None:
    facts(env).update_balance(ACCOUNT, QuoteBalance(Decimal(free), Decimal(locked), env.clock.now()))


def open_day(env: M2Env, usdt: str = "10000") -> None:
    """가상 일 경계 직후: 기준선(D-103, 사람 확정 가정) + 거래소 잔고 조회 + 기준 금액 고정."""
    f = facts(env)
    if f.baseline(ACCOUNT) is None:
        f.set_baseline(ACCOUNT, Decimal(usdt), env.clock.now())
    balance(env, usdt)
    assert f.start_day(ACCOUNT) is not None


def fee(amount: Decimal) -> Decimal:
    return estimate_cost(amount, False, M2_POLICY.cost_rates)


# --- D-118: M2 비용 추정은 quoteAssetPrecision 자릿수 올림, M1은 원 단위 올림 그대로 ---------------------------

def test_d118_cost_estimate_rounds_up_to_quote_precision() -> None:
    assert M2_POLICY.cost_rates.cost_quantum == Decimal("0.00000001")
    assert estimate_cost(Decimal("1005"), False, M2_POLICY.cost_rates) == Decimal("1.00500000")
    assert estimate_cost(Decimal("0.123456789"), False, M2_POLICY.cost_rates) == Decimal("0.00012346")  # 올림
    m1 = CostRates(commission_rate=Decimal("0.00015"), sell_tax_rate=Decimal("0.0018"))
    assert m1.cost_quantum is None
    assert estimate_cost(Decimal("1005"), False, m1) == Decimal("1")  # 0.15075 → 원 미만 올림 (M1 불변)
    assert str(estimate_cost(Decimal("1000000"), True, m1)) == "1950"


# --- V-151 정상: 매수 체결 즉시 USDT 감소, 이중 차감 없음 ----------------------------------------------------

def test_v151_buy_fill_reduces_usdt_immediately(env: M2Env) -> None:
    at(env, T0)
    open_day(env)
    f = facts(env)
    before = f.cash(ACCOUNT)
    assert before is not None and before.deposit == Decimal("10000") and before.broker_orderable == Decimal("10000")

    d = run(env, signal(env, RequestType.ENTRY))  # 0.1000 × 10050.00 = 1005 USDT, 수수료는 기초 자산(TSTA)
    assert d.result is GateResult.APPROVED, d.reason
    assert env.repo.active_reservations(ACCOUNT) == []  # 전량 체결 → 예약 해제
    spent = Decimal("1005.0000")
    # 거래소 잔고를 아직 다시 조회하지 않았어도(free = 10000 그대로) 내부 기대 USDT가 체결 즉시 줄어든다
    after = f.cash(ACCOUNT)
    assert after is not None
    assert after.deposit == Decimal("10000") - spent - fee(spent)  # 수수료 자산 미기록(T-M2-04 전) → 보수 추정 차감
    assert after.broker_orderable == Decimal("10000")
    assert f.expected_quote(ACCOUNT) == after.deposit


def test_v151_no_t2_double_deduction_after_exchange_refresh(env: M2Env) -> None:
    at(env, T0)
    open_day(env)
    assert run(env, signal(env, RequestType.ENTRY)).result is GateResult.APPROVED
    spent = Decimal("1005.0000")
    at(env, T0 + timedelta(seconds=30))
    balance(env, str(Decimal("10000") - spent))  # 거래소 free는 체결을 이미 반영(즉시 반영)
    view = facts(env).cash(ACCOUNT)
    assert view is not None
    assert view.broker_orderable == Decimal("8995.0000")
    # M1 방식(조회값 − 당일 매수 체결 전액)이면 7,990 근처가 된다. M2는 다시 빼지 않는다
    assert view.deposit == Decimal("10000") - spent - fee(spent)
    assert min(view.broker_orderable, view.deposit) > Decimal("8990")


def test_v151_gate_uses_min_of_exchange_free_and_internal(env: M2Env) -> None:
    """주문 가능 = min(free, 내부 기대 − 활성 매수 예약) ≥ 주문 금액 + 추정 수수료(D-118 올림)."""
    at(env, T0)
    open_day(env)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY  # 미체결 → 활성 매수 예약으로 남음
    first = run(env, signal(env, RequestType.ENTRY, "TSTB-USDT", "800.0", "2.5000", "e1"))  # 2,000 USDT 예약
    assert first.result is GateResult.APPROVED, first.reason
    reserved = sum((r.cash_remaining for r in env.repo.active_reservations(ACCOUNT)), Decimal("0"))
    assert reserved == Decimal("2000.0000") + fee(Decimal("2000.0000"))
    # 거래소 free는 미체결 매수 금액만큼 locked로 옮겨 감 (가상): free 8,000, locked 2,000
    balance(env, "8000", "2000")
    d = run(env, signal(env, RequestType.ENTRY, "TSTC-USDT", "10.0", "2.5000", "e2"))
    snap = json.loads(env.repo.select("SELECT limit_snapshot FROM gate_decision WHERE decision_id=?",
                                      (d.decision_id,))[0]["limit_snapshot"])
    assert Decimal(snap["cash_available"]) == min(Decimal("8000"), Decimal("10000") - reserved)
    assert Decimal(snap["cash_need"]) == Decimal("25.00000") + fee(Decimal("25.00000"))


# --- V-151 오류: 현금 부족 매수 거부(G8), 전송 0 -------------------------------------------------------------

def test_v151_insufficient_exchange_free_rejected_g8(env: M2Env) -> None:
    at(env, T0)
    open_day(env)
    balance(env, "1005")  # 거래소 free < 1005 + 추정 수수료
    d = run(env, signal(env, RequestType.ENTRY))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G8
    assert env.broker.sent == []


def test_v151_insufficient_internal_expected_rejected_g8(env: M2Env) -> None:
    at(env, T0)
    f = facts(env)
    f.set_baseline(ACCOUNT, Decimal("1005"), env.clock.now())  # 내부 기대 USDT가 작음
    balance(env, "10000")
    f.start_day(ACCOUNT)
    d = run(env, signal(env, RequestType.ENTRY))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G8
    assert env.broker.sent == []


@pytest.mark.parametrize(("short", "approved"), [(Decimal("0"), True), (Decimal("0.00000001"), False)])
def test_v151_exact_cash_boundary_with_rounded_up_fee(env: M2Env, short: Decimal, approved: bool) -> None:
    """필요 = 1,005 + 추정 수수료(1.005, D-118 올림). 거래소 free·내부 기대 각각 경계에서 판정."""
    at(env, T0)
    need = Decimal("1005.0000") + fee(Decimal("1005.0000"))
    facts(env).set_baseline(ACCOUNT, need - short, env.clock.now())
    balance(env, str(need), str(Decimal("10000") - need))  # free = 필요 금액, 나머지는 locked(기준 금액 10,000 유지)
    assert facts(env).start_day(ACCOUNT) is not None
    d = run(env, signal(env, RequestType.ENTRY))
    assert (d.result is GateResult.APPROVED) is approved, d.reason
    if not approved:
        assert d.failed_check is Check.G8 and env.broker.sent == []


@pytest.mark.parametrize("case", ["no_balance", "stale_balance", "no_baseline"])
def test_v151_unknown_cash_rejects_risk_increase(tmp_path: Path, case: str) -> None:
    env = build_m2_env(tmp_path, spot_cash=True)
    try:
        f = facts(env)
        at(env, T0)
        if case != "no_baseline":
            f.set_baseline(ACCOUNT, Decimal("10000"), env.clock.now())
        if case != "no_balance":
            balance(env, "10000")
        f.start_day(ACCOUNT)
        if case == "stale_balance":
            at(env, T0 + MAX_AGE + timedelta(seconds=1))
        assert f.cash(ACCOUNT) is None
        d = run(env, signal(env, RequestType.ENTRY))
        assert d.result is GateResult.REJECTED and d.failed_check in (Check.G7, Check.G8)
        assert env.broker.sent == []
    finally:
        env.repo.close()


def test_v151_sell_not_blocked_by_unknown_cash(env: M2Env) -> None:
    """위험 축소(청산)는 G8을 거치지 않는다 — 현금을 몰라도 매도 허용 (CLAUDE.md §8-3)."""
    at(env, T0)
    open_day(env)
    assert run(env, signal(env, RequestType.ENTRY)).result is GateResult.APPROVED
    at(env, T0 + MAX_AGE + timedelta(minutes=1))  # 잔고 조회 신선도 초과 → 현금 모름
    assert facts(env).cash(ACCOUNT) is None
    out = run(env, signal(env, RequestType.EXIT, quantity="0.0999", evaluation="eval-2"))
    assert out.result is GateResult.APPROVED, out.reason
    # 매도 수령액(− 추정 수수료)이 내부 기대 USDT에 반영된다
    sold = Decimal("0.0999") * RULES["TSTA-USDT"].last
    expected = Decimal("10000") - Decimal("1005.0000") - fee(Decimal("1005.0000")) + sold - fee(sold)
    assert facts(env).expected_quote(ACCOUNT) == expected


# --- V-140 기준 금액·L4: UTC 00:00 경계, KST 자정 무관 --------------------------------------------------------

def test_v140_base_amount_is_usdt_free_locked_plus_holdings(env: M2Env) -> None:
    at(env, T0)
    open_day(env)
    assert run(env, signal(env, RequestType.ENTRY)).result is GateResult.APPROVED
    f = facts(env)
    # 다음 날 UTC 00:00 직후 거래소 잔고 조회 → 새 기준 금액 = free + locked + 보유 평가액
    at(env, DAY_START + timedelta(days=1, seconds=30))
    balance(env, "8000", "995")
    base = f.start_day(ACCOUNT)
    held = env.repo.position(ACCOUNT, "TSTA-USDT").quantity
    assert base == Decimal("8995") + held * RULES["TSTA-USDT"].last
    risk = f.daily_risk(ACCOUNT)
    assert risk is not None and risk.base_amount == base
    rec = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "DAILY_BASE"]
    assert rec[-1]["market"] == "M2" and rec[-1]["trade_date"] == "2026-10-08"


def test_v140_start_day_requires_balance_after_boundary(env: M2Env) -> None:
    at(env, DAY_START + timedelta(days=1) - timedelta(seconds=10))  # 전날 UTC 23:59:50 조회
    facts(env).set_baseline(ACCOUNT, Decimal("10000"), env.clock.now())
    balance(env, "10000")
    at(env, DAY_START + timedelta(days=1, seconds=20))
    assert facts(env).start_day(ACCOUNT) is None  # 일 경계 이전 잔고로 기준 금액을 정하지 않음
    assert facts(env).daily_risk(ACCOUNT) is None


def test_v140_start_day_refuses_when_fill_after_balance_query(env: M2Env) -> None:
    """재검토 높음(2026-10-08): 잔고 조회(bal.at)와 start_day 사이 체결 → 현금(조회 시점)과 평가액(현재)이 어긋나
    매도면 가짜 이익으로 L4가 가려진다. 그 사이 체결이 있으면 기준 금액을 정하지 않는다(다음 조회 뒤 다시)."""
    at(env, T0)
    open_day(env)
    assert run(env, signal(env, RequestType.ENTRY)).result is GateResult.APPROVED
    held = env.repo.position(ACCOUNT, "TSTA-USDT").quantity
    f = facts(env)
    at(env, DAY_START + timedelta(days=1, seconds=10))
    balance(env, "8995")
    at(env, DAY_START + timedelta(days=1, seconds=20))
    sold = run(env, signal(env, RequestType.EXIT, quantity=str(held), evaluation="e-exit"))
    assert sold.result is GateResult.APPROVED, sold.reason
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == 0
    at(env, DAY_START + timedelta(days=1, seconds=30))
    assert f.start_day(ACCOUNT) is None
    assert f.daily_risk(ACCOUNT) is None  # 기준 미고정 → 위험 확대 거부
    assert not [e for e in env.repo.audit_events() if e["kind"] == "DAILY_BASE"
                and json.loads(e["summary"])["trade_date"] == "2026-10-08"]
    # 체결 뒤 새로 조회한 잔고로는 정한다: 현금·평가액이 같은 시점 → 당일 손익 0에서 시작
    balance(env, "9993")
    base = f.start_day(ACCOUNT)
    assert base == Decimal("9993")
    risk = f.daily_risk(ACCOUNT)
    assert risk is not None and risk.loss_ratio == 0


def test_v140_l4_daily_loss_splits_at_utc_midnight_not_kst(env: M2Env) -> None:
    at(env, T0)
    open_day(env)
    assert run(env, signal(env, RequestType.ENTRY, quantity="0.2000", price="10000.00")).result is GateResult.APPROVED
    env.market.prices["TSTA-USDT"] = Decimal("8000.00")  # 평가 손실 (가상)
    risk = facts(env).daily_risk(ACCOUNT)
    assert risk is not None and risk.loss_ratio >= Decimal("0.02")
    blocked = run(env, signal(env, RequestType.ENTRY, "TSTB-USDT", "10.0", "2.5000", "e2"))
    assert blocked.result is GateResult.REJECTED and "L4" in (blocked.reason or "")

    # KST 자정(UTC 15:00)을 지나도 같은 UTC 거래일 → 여전히 L4
    at(env, datetime(2026, 10, 7, 15, 30, tzinfo=UTC))
    balance(env, "8000")
    again = run(env, signal(env, RequestType.ENTRY, "TSTB-USDT", "10.0", "2.5000", "e3"))
    assert again.result is GateResult.REJECTED and "L4" in (again.reason or "")

    # UTC 00:00이 지나면 새 거래일: 기준 금액을 고정하기 전에는 위험 확대 거부(모름), 고정 후 L4 초기화
    at(env, DAY_START + timedelta(days=1, minutes=15))
    assert facts(env).daily_risk(ACCOUNT) is None
    pending = run(env, signal(env, RequestType.ENTRY, "TSTB-USDT", "10.0", "2.5000", "e4"))
    assert pending.result is GateResult.REJECTED and pending.failed_check is Check.G7
    balance(env, "8000")
    assert facts(env).start_day(ACCOUNT) is not None
    risk2 = facts(env).daily_risk(ACCOUNT)
    assert risk2 is not None and risk2.loss_ratio == 0
    ok = run(env, signal(env, RequestType.ENTRY, "TSTB-USDT", "10.0", "2.5000", "e5"))
    assert ok.result is GateResult.APPROVED, ok.reason


def test_v140_daily_base_survives_restart_same_utc_day(tmp_path: Path) -> None:
    """재시작해도 같은 UTC 거래일의 기준 금액을 다시 쓴다 — 재시작으로 그날 손실이 L4에서 사라지지 않게."""
    env = build_m2_env(tmp_path, spot_cash=True)
    at(env, T0)
    open_day(env)
    first = facts(env).daily_risk(ACCOUNT)
    assert first is not None
    env.repo.close()

    repo = Repository(tmp_path / "trading.db", Mode.BACKTEST)
    try:
        f2 = SpotAccountFacts(repo, env.clock, quote_asset=QUOTE_ASSET, asset_pair=asset_pair,
                              price_of=lambda s: RULES[s].last, cost_rates=M2_POLICY.cost_rates,
                              reference_time=env_limits_reference(), max_age=MAX_AGE, market_time=M2_TIME)
        at(env, T0 + timedelta(hours=2))
        f2.update_balance(ACCOUNT, QuoteBalance(Decimal("12000"), Decimal("0"), env.clock.now()))  # 외부 입금 등
        assert f2.start_day(ACCOUNT) == first.base_amount  # 저장된 기준 사용
        assert f2.baseline(ACCOUNT) == Decimal("10000")     # 기대 USDT 기준선도 복원
    finally:
        repo.close()


def env_limits_reference():  # type: ignore[no-untyped-def]
    from tests.support.m2_fakes import m2_limits
    return m2_limits().daily_reference_time


# --- 시장별 분리: M1 현금 정책·일일 리스크 코드는 M2 구성에 쓰이지 않는다 ----------------------------------------

def test_m2_account_facts_is_not_m1_conservative_cash(env: M2Env) -> None:
    from trading_system.monitoring.watchers import ConservativeCashFacts, DailyRiskTracker
    assert not isinstance(env.account, (ConservativeCashFacts, DailyRiskTracker))
    assert FEE_RATE == M2_POLICY.cost_rates.commission_rate and M2_POLICY.cost_rates.sell_tax_rate == 0
