"""T-M2-04 체결 수수료·순 수령·dust·소유권 해제 사유, 스키마 전진 변경 (CP-024 D-101·D-102·D-118, 승인 A-066·A-071).

검증: V-129(수수료 기록·순 수령·대조 일치), V-130(미승인 수수료 자산 → K4, 기록 없음), V-131(dust → 포지션 없음 취급·
소유권 해제 "dust 확정"·L6 제외·대조 일치), V-132(dust 청산 생략 기록 + 알림, 전송 0), V-160(minQty 미만 전송 없이 dust /
금액 경계 전송 → -1013이면 dust 확정 + 알림 / 시세 지연 중 LOT_SIZE·지정가 기준), D-118(거래소 보고 금액 Y·quoteQty,
fills만 있으면 임시값 후 확정, cummulativeQuoteQty < 0 = 데이터 없음), V-152(기존 M1 DB가 그대로 열림 — 추가 표만).
모든 값은 가상이다(tests/support/m2_fakes.py). 네트워크 없음. 매 시험 끝에 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT as M1_ACCOUNT
from tests.scenario.conftest import build_env as build_m1_env
from tests.scenario.conftest import make_signal as m1_signal
from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, RULES, T0, M2Env, build_m2_env, ioc_buy_price
from trading_system.broker.fake.broker import FakeBehavior, FakeSpotFees
from trading_system.domain.enums import Check, GateResult, IntentState, Mode, OrderType, RequestType
from trading_system.domain.models import GateRequest, OrderEvent, Signal
from trading_system.monitoring.spot_cash import QuoteBalance
from trading_system.reconciliation.reconciler import Reconciler
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.maintenance import backup, migrate, restore_test, schema_version_of
from trading_system.storage.repository import SCHEMA_VERSION, Repository

STRATEGY = "tm204-fixed"  # 테스트 전용 고정 전략 (실제 매매전략 아님)
D = Decimal


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def signal(kind: RequestType, instrument: str = "TSTA-USDT", quantity: str = "0.1000", price: str | None = None,
           evaluation: str = "eval-1", t=None) -> Signal:  # type: ignore[no-untyped-def]
    rule = RULES[instrument]
    buying = kind in (RequestType.ENTRY, RequestType.INCREASE)
    otype = OrderType.LIMIT if buying else OrderType.MARKET
    px = D(price) if price is not None else (ioc_buy_price(rule.last, rule.tick_size) if buying else rule.last)
    now = t or T0
    return Signal(strategy_id=STRATEGY, strategy_version=1, evaluation_id=evaluation, account=ACCOUNT,
                  instrument=instrument, rule_id="R1", kind=kind, quantity=D(quantity), order_type=otype,
                  price=px, as_of=now - timedelta(minutes=1), valid_until=now + timedelta(minutes=30))


def run(env: M2Env, sig: Signal):  # type: ignore[no-untyped-def]
    req = GateRequest.from_signal(sig)
    if env.dust.screen_exit(req):
        return None   # dust 확정 — 게이트·전송 없음 (D-102)
    decision = env.gate.decide(req)
    if decision.approved is not None:
        env.executor.submit(decision.approved)
        env.executor.process_events()
    return decision


def reconcile(env: M2Env) -> str:
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock)) \
        .run(ACCOUNT, "PERIODIC").result


def fee_rows(env: M2Env) -> list[dict]:
    return env.repo.select("SELECT * FROM fill_fee ORDER BY recorded_at, fill_key")


def alerts(env: M2Env) -> list[str]:
    return [json.loads(e["summary"])["message"] for e in env.repo.audit_events() if e["kind"] == "ALERT"]


def released(env: M2Env, instrument: str = "TSTA-USDT") -> list[dict]:
    return env.repo.select("SELECT * FROM ownership WHERE account=? AND instrument=? AND state='RELEASED'",
                           (ACCOUNT, instrument))


def hold(env: M2Env, quantity: str) -> None:
    """가상 보유 만들기: 진입 후 (총 체결 − 수수료) 순 수령. quantity = 매수 주문 수량."""
    d = run(env, signal(RequestType.ENTRY, quantity=quantity, evaluation=f"hold-{quantity}"))
    assert d is not None and d.result is GateResult.APPROVED, d and d.reason


# --- V-129 + D-118: 체결 기록에 수수료·수수료 자산·거래소 보고 금액 ------------------------------------------------

def test_v129_fill_records_fee_asset_and_reported_quote(env: M2Env) -> None:
    hold(env, "0.1000")
    [row] = fee_rows(env)
    assert (row["commission"], row["commission_asset"]) == ("0.0001000", "TSTA")
    assert row["quote_status"] == "CONFIRMED" and D(row["quote_qty"]) == D("1005.0000")
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0999")   # 순 수령
    assert reconcile(env) == "MATCH"


def test_d118_provisional_quote_then_confirmed_without_double_count(env: M2Env) -> None:
    """FULL 응답 fills만 있을 때 price × qty 임시값 → 사용자 스트림 Y로 확정. 같은 체결 ID는 한 번만 반영."""
    env.broker.provisional_quotes = True
    env.broker.quote_report = lambda qty, px: (qty * px - D("0.00000123"))   # 거래소 보고 금액(가상, 반올림 차이)
    hold(env, "0.1000")
    [row] = fee_rows(env)
    assert row["quote_status"] == "PROVISIONAL" and D(row["quote_qty"]) == D("1005.0000")
    env.broker.confirm_quotes()
    env.executor.process_events()
    [row] = fee_rows(env)
    assert row["quote_status"] == "CONFIRMED" and D(row["quote_qty"]) == D("1004.99999877")
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0999")   # 이중 반영 없음
    assert env.repo.halt_rows(ACCOUNT) == []


def test_d118_conflicting_confirmed_quote_raises_k4(env: M2Env) -> None:
    hold(env, "0.1000")
    [fill] = env.repo.select("SELECT f.fill_id, b.order_no FROM fill f JOIN broker_order b ON b.order_key=f.order_key")
    env.broker.inject(OrderEvent(kind="FILL", order_no=fill["order_no"], fill_id=fill["fill_id"],
                                 quantity=D("0.1000"), price=D("10050.00"), commission=D("0.0001000"),
                                 commission_asset="TSTA", instrument="TSTA-USDT", quote_qty=D("1006"),
                                 quote_confirmed=True))
    env.executor.process_events()
    assert [r["halt_type"] for r in env.repo.halt_rows(ACCOUNT)] == ["K4"]
    assert D(fee_rows(env)[0]["quote_qty"]) == D("1005.0000")   # 원장은 바꾸지 않음


def test_d118_negative_cumulative_quote_is_no_data_not_zero(env: M2Env) -> None:
    """주문 조회 cummulativeQuoteQty < 0 = 데이터 없음 — 0으로 보지 않고 임시값(price × qty)으로 둔다."""
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, signal(RequestType.ENTRY))
    assert d is not None and d.result is GateResult.APPROVED
    [no] = env.broker.open_order_nos()
    env.broker.inject(OrderEvent(kind="FILL", order_no=no, fill_id="Fneg-1", quantity=D("0.1000"),
                                 price=D("10050.00"), commission=D("0.0001"), commission_asset="TSTA",
                                 instrument="TSTA-USDT", quote_qty=D("-1"), quote_confirmed=True))
    env.executor.process_events()
    [row] = fee_rows(env)
    assert row["quote_status"] == "PROVISIONAL" and D(row["quote_qty"]) == D("1005.0000")


def test_m1_fill_without_fee_has_no_fee_row(tmp_path: Path) -> None:
    """M1 동작 불변: 수수료·보고 금액이 없는 통보(키움)는 추가 표에 쓰지 않는다."""
    e = build_m1_env(tmp_path)
    try:
        d = e.gate.decide(GateRequest.from_signal(m1_signal(RequestType.ENTRY)))
        e.executor.submit(d.approved)
        e.executor.process_events()
        assert e.repo.position(M1_ACCOUNT, "TST-A").quantity == D("100")
        assert e.repo.select("SELECT * FROM fill_fee") == []
        assert check_invariants(e.repo.connection, Mode.BACKTEST.value) == []
    finally:
        e.repo.close()


# --- V-130: 수수료 자산이 기초·USDT 외 → K4, 체결·수수료 기록 없음 --------------------------------------------

def test_v130_unapproved_fee_asset_k4_without_ledger_change(env: M2Env) -> None:
    env.broker.spot_fees = FakeSpotFees(rate=D("0.001"), pairs=env.broker.spot_fees.pairs, asset_override="TSTFEE")
    d = run(env, signal(RequestType.ENTRY))
    assert d is not None and d.result is GateResult.APPROVED
    assert [r["halt_type"] for r in env.repo.halt_rows(ACCOUNT)] == ["K4"]
    assert env.repo.select("SELECT * FROM fill") == [] and fee_rows(env) == []
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == 0


# --- USDT 흐름은 기록된 수수료·보고 금액으로 (D-103·D-118, spot_cash [제안](1) 해소) ---------------------------------

def test_quote_flow_uses_recorded_fee_and_reported_amount(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, spot_cash=True)
    try:
        e.broker.quote_report = lambda qty, px: (qty * px - D("0.00000123"))
        f = e.account
        f.set_baseline(ACCOUNT, D("10000"), e.clock.now())
        f.update_balance(ACCOUNT, QuoteBalance(D("10000"), D("0"), e.clock.now()))
        assert f.start_day(ACCOUNT) is not None
        hold(e, "0.1000")   # 보고 금액 1004.99999877, 수수료 = 기초 자산(TSTA) → USDT 수수료 없음
        assert f.expected_quote(ACCOUNT) == D("10000") - D("1004.99999877")
        e.clock.advance(timedelta(minutes=1))   # 기준선 시각과 같은 시각의 매도는 흐름에서 제외([제안](2)) — 1분 뒤 매도
        out = run(e, signal(RequestType.EXIT, quantity="0.0999", evaluation="x1"))
        assert out is not None and out.result is GateResult.APPROVED
        sold = D("0.0999") * D("10000.00") - D("0.00000123")
        usdt_fee = D("0.0999") * D("10000.00") * D("0.001")   # 매도 수수료 = USDT(가상 요율)
        assert f.expected_quote(ACCOUNT) == D("10000") - D("1004.99999877") + sold - usdt_fee
    finally:
        e.repo.close()


# --- V-131·V-132: dust 청산 생략 — 기록·알림·소유권 해제, 전송 0, L6 제외, 대조 일치 ---------------------------------

def make_dust(env: M2Env) -> None:
    """보유 0.0999 → 0.0990 매도 → 남은 0.0009 (가상 minQty 0.0010 미만 = dust)."""
    env.rules.table["TSTA-USDT"] = _rules_min_qty(env, D("0.0010"))
    hold(env, "0.1000")
    out = run(env, signal(RequestType.REDUCE, quantity="0.0990", evaluation="r1"))
    assert out is not None and out.result is GateResult.APPROVED, out.reason
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0009")


def _rules_min_qty(env: M2Env, min_qty: Decimal):  # type: ignore[no-untyped-def]
    from dataclasses import replace
    return replace(env.rules.table["TSTA-USDT"], lot_min_qty=min_qty, market_min_qty=min_qty)


def test_v132_dust_exit_not_sent_recorded_alerted_and_released(env: M2Env) -> None:
    make_dust(env)
    sent = len(env.broker.sent)
    assert run(env, signal(RequestType.EXIT, quantity="0.0009", evaluation="x1")) is None
    assert len(env.broker.sent) == sent                                  # 전송 0
    [ev] = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "DUST_CONFIRMED"]
    assert ev["instrument"] == "TSTA-USDT" and ev["verdict"] == "DUST_QUANTITY" and ev["quantity"] == "0.0009"
    assert any("dust" in m and "전송 안 함" in m for m in alerts(env))
    [own] = released(env)
    assert own["released_reason"] == "dust 확정" and own["released_at"] is not None
    assert env.repo.active_ownership(ACCOUNT, "TSTA-USDT") is None
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0009")   # 원장에는 유지
    assert reconcile(env) == "MATCH"                                     # 대조에도 유지 → 일치 (V-131)


def test_v131_dust_excluded_from_l6_and_reentry_allowed(env: M2Env) -> None:
    from tests.support.m2_fakes import m2_limits
    env.gate._in.limits.by_account[ACCOUNT] = m2_limits(l6_max_positions=1)   # noqa: SLF001 — 가상 L6 = 1종목
    make_dust(env)
    run(env, signal(RequestType.EXIT, quantity="0.0009", evaluation="x1"))   # dust 확정
    other = run(env, signal(RequestType.ENTRY, "TSTB-USDT", "100.0", evaluation="b1"))
    assert other is not None and other.result is GateResult.APPROVED, other.reason   # dust 종목은 L6에 세지 않음
    again = run(env, signal(RequestType.ENTRY, "TSTA-USDT", "0.1000", evaluation="a2"))
    assert again is not None and again.result is GateResult.REJECTED and again.failed_check is Check.G7  # L6(1) 초과
    assert "L6" in (again.reason or "")


def test_v131_dust_reentry_after_release(env: M2Env) -> None:
    make_dust(env)
    run(env, signal(RequestType.EXIT, quantity="0.0009", evaluation="x1"))
    d = run(env, signal(RequestType.ENTRY, quantity="0.1000", evaluation="a2"))
    assert d is not None and d.result is GateResult.APPROVED, d.reason   # 소유권 해제 → 재진입 가능
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == D("0.0009") + D("0.0999")


def test_dust_review_after_partial_sell_releases_ownership(env: M2Env) -> None:
    """매도 종결 뒤 남은 잔량이 확실한 dust면 그때 소유권을 해제한다(청산 신호를 기다리지 않음)."""
    make_dust(env)
    assert env.dust.review(ACCOUNT, "TSTA-USDT") is True
    assert released(env)[0]["released_reason"] == "dust 확정"
    assert env.dust.review(ACCOUNT, "TSTA-USDT") is False   # 이미 해제 — 중복 기록 없음
    assert len([e for e in env.repo.audit_events() if e["kind"] == "DUST_CONFIRMED"]) == 1


def test_dust_not_confirmed_while_sell_intent_active(env: M2Env) -> None:
    make_dust(env)
    assert env.dust.review(ACCOUNT, "TSTB-USDT") is False   # 보유 없음
    env.repo.connection.execute("UPDATE intent SET state='UNKNOWN' WHERE side='SELL'")  # 가상: 결과 불명 매도 남음
    assert env.dust.review(ACCOUNT, "TSTA-USDT") is False   # 상태 불확실 → 확정하지 않음
    env.repo.connection.execute("UPDATE intent SET state='FILLED' WHERE side='SELL'")


def test_normal_exit_is_not_dust(env: M2Env) -> None:
    hold(env, "0.1000")
    out = run(env, signal(RequestType.EXIT, quantity="0.0999", evaluation="x1"))
    assert out is not None and out.result is GateResult.APPROVED
    assert not [e for e in env.repo.audit_events() if e["kind"] == "DUST_CONFIRMED"]
    assert released(env)[0]["released_reason"] == "전량 청산"


# --- V-160: 금액 경계 → 전송, -1013이면 dust 확정 + 알림 / 시세 지연 중 LOT_SIZE·지정가 기준 -------------------------

def _boundary(env: M2Env) -> None:
    """보유 0.0999, 시세 50.05 → 명목가 약 5.0 (가상 minNotional 5 경계). 참조가 중 하나는 5 미만."""
    hold(env, "0.1000")
    env.market.prices["TSTA-USDT"] = D("50.06")
    env.rules.extra_prices["TSTA-USDT"] = (D("50.00"),)   # avgPrice(가상): 0.0999 × 50.00 = 4.995 < 5


def test_v160_boundary_sell_is_sent_then_minus1013_confirms_dust(env: M2Env) -> None:
    _boundary(env)
    env.broker.behavior, env.broker.reject_code = FakeBehavior.REJECT, -1013
    out = run(env, signal(RequestType.EXIT, quantity="0.0999", price="50.06", evaluation="x1"))
    assert out is not None and out.result is GateResult.APPROVED, out.reason      # 미리 막지 않음
    assert env.broker.sent[-1].side == "SELL"                                      # 전송됨
    assert env.repo.intent(out.approved.intent_id).state is IntentState.REJECTED
    [ev] = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "DUST_CONFIRMED"]
    assert ev["cause"] == "-1013 거부" and ev["verdict"] == "DUST_NOTIONAL"
    assert released(env)[0]["released_reason"] == "dust 확정"
    assert any("dust" in m for m in alerts(env))


def test_v160_minus1013_outside_boundary_is_not_dust(env: M2Env) -> None:
    hold(env, "0.1000")   # 명목가 약 999 USDT — 경계 아님
    env.broker.behavior, env.broker.reject_code = FakeBehavior.REJECT, -1013
    out = run(env, signal(RequestType.EXIT, quantity="0.0999", evaluation="x1"))
    assert out is not None and env.repo.intent(out.approved.intent_id).state is IntentState.REJECTED
    assert not [e for e in env.repo.audit_events() if e["kind"] == "DUST_CONFIRMED"]
    assert env.repo.active_ownership(ACCOUNT, "TSTA-USDT") is not None


def test_v160_other_reject_code_is_not_dust(env: M2Env) -> None:
    _boundary(env)
    env.broker.behavior, env.broker.reject_code = FakeBehavior.REJECT, -2010
    run(env, signal(RequestType.EXIT, quantity="0.0999", price="50.06", evaluation="x1"))
    assert not [e for e in env.repo.audit_events() if e["kind"] == "DUST_CONFIRMED"]


def test_v160_certain_notional_dust_not_sent(env: M2Env) -> None:
    hold(env, "0.1000")
    env.market.prices["TSTA-USDT"] = D("40.00")   # 0.0999 × 40 = 3.996 < 5, 다른 참조가 없음
    sent = len(env.broker.sent)
    assert run(env, signal(RequestType.EXIT, quantity="0.0999", price="40.00", evaluation="x1")) is None
    assert len(env.broker.sent) == sent
    [ev] = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "DUST_CONFIRMED"]
    assert ev["verdict"] == "DUST_NOTIONAL"


def test_v160_stale_quote_uses_lot_size(env: M2Env) -> None:
    """시세 지연 중: IOC 지정가로 보낼 것이므로 LOT_SIZE minQty로 판정. MARKET_LOT_SIZE로는 dust여도 전송 대상."""
    from dataclasses import replace
    hold(env, "0.1000")
    env.rules.table["TSTA-USDT"] = replace(env.rules.table["TSTA-USDT"], market_min_qty=D("1"))
    env.market.quote_age["TSTA-USDT"] = timedelta(seconds=120)   # 지연
    req = GateRequest.from_signal(signal(RequestType.EXIT, quantity="0.0999", evaluation="x1"))
    assert env.dust.screen_exit(req) is False
    env.market.quote_age.pop("TSTA-USDT")                        # 정상 시세 → 시장가 경로 → MARKET_LOT_SIZE
    assert env.dust.screen_exit(req) is True


def test_reduce_smaller_than_position_is_not_screened(env: M2Env) -> None:
    """감액(일부 매도)은 dust 판정 대상이 아니다 — 수량이 규칙 위반이면 게이트 G10이 거부."""
    hold(env, "0.1000")
    req = GateRequest.from_signal(signal(RequestType.REDUCE, quantity="0.0500", evaluation="r1"))
    assert env.dust.screen_exit(req) is False


def test_rules_unknown_does_not_confirm_dust(env: M2Env) -> None:
    make_dust(env)
    del env.rules.table["TSTA-USDT"]   # 규칙 미적재 → 판정 불가 → 확정하지 않음 (위험 축소는 게이트로)
    req = GateRequest.from_signal(signal(RequestType.EXIT, quantity="0.0009", evaluation="x1"))
    assert env.dust.screen_exit(req) is False


# --- V-152 스키마: 추가 표만(전진), 기존 M1 DB가 그대로 열리고 M1 동작 유지 ---------------------------------------------

def test_existing_m1_db_opens_unchanged_and_gets_additive_table(tmp_path: Path) -> None:
    e = build_m1_env(tmp_path)
    d = e.gate.decide(GateRequest.from_signal(m1_signal(RequestType.ENTRY)))
    e.executor.submit(d.approved)
    e.executor.process_events()
    e.repo.close()
    db = tmp_path / "trading.db"
    # T-M2-04 이전 DB 재현: 추가 표가 없는 스키마 버전 2
    conn = sqlite3.connect(db)
    conn.execute("DROP TABLE fill_fee")
    counts = {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
              for t in ("intent", "attempt", "broker_order", "fill", "position", "audit_event")}
    conn.commit()
    conn.close()
    assert schema_version_of(db) == SCHEMA_VERSION == "2"

    saved = backup(db, tmp_path / "backup")                   # 적용 전 백업 (03 D-060)
    assert restore_test(saved, Mode.BACKTEST.value).ok
    assert migrate(db, tmp_path / "backup") == []             # 버전 단계 없음 — 기존 마이그레이션 체계와 충돌 없음
    repo = Repository(db, Mode.BACKTEST)                      # 그대로 열림 (SchemaMismatch 없음)
    try:
        assert repo.select("SELECT name FROM sqlite_master WHERE type='table' AND name='fill_fee'")
        assert {t: repo.select(f"SELECT COUNT(*) AS c FROM {t}")[0]["c"] for t in counts} == counts
        assert schema_version_of(db) == "2"
        assert repo.position(M1_ACCOUNT, "TST-A").quantity == D("100")
        assert check_invariants(repo.connection, Mode.BACKTEST.value) == []
    finally:
        repo.close()
