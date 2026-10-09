"""T-M2-01 최소 종단 간 흐름 — M2판 (CP-024 §6, 승인 A-066, 구현 시작 A-071).

가상 크립토 심볼·소수 수량·기초 자산 수수료 차감 → 신호 → 게이트 → fake 브로커 → 기록, 최소 킬 스위치·주문 차단.
검증: V-129(순 수령 포지션·대조 일치), V-130(미승인 수수료 자산 → K4, 최소), V-01(L2·L3 소수 경계), V-04, V-07,
V-12, V-20(K1 부분·KILL 파일), V-26, V-55, G1(모드·환경 불일치), G9(순 수령 초과 매도), G10(stepSize·tickSize).
모든 값은 가상이다(tests/support/m2_fakes.py). 네트워크 없음. 매 시험 끝에 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import threading
from collections.abc import Iterator
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, RULES, T0, M2Env, build_m2_env, ioc_buy_price
from trading_system.broker.fake.broker import FakeBehavior, FakeSpotFees
from trading_system.domain.enums import (
    Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.models import Account, GateRequest, Signal
from trading_system.reconciliation.reconciler import Reconciler
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import ModeMismatch, StorageFailure

STRATEGY = "tm201-fixed"  # 테스트 전용 고정 전략 (M1 t001-fixed와 같은 성격, 실제 매매전략 아님)


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def signal(kind: RequestType, instrument: str = "TSTA-USDT", quantity: str = "0.1000", price: str | None = None,
           evaluation: str = "eval-1", order_type: OrderType | None = None,
           valid_for: timedelta = timedelta(minutes=30)) -> Signal:
    """매수는 IOC 지정가 가격 규칙(D-104, 시험 계산), 매도는 시장가(D-104)가 기본."""
    rule = RULES[instrument]
    buying = kind in (RequestType.ENTRY, RequestType.INCREASE)
    otype = order_type or (OrderType.LIMIT if buying else OrderType.MARKET)
    px = Decimal(price) if price is not None else (ioc_buy_price(rule.last, rule.tick_size) if buying else rule.last)
    return Signal(strategy_id=STRATEGY, strategy_version=1, evaluation_id=evaluation, account=ACCOUNT,
                  instrument=instrument, rule_id="R1", kind=kind, quantity=Decimal(quantity), order_type=otype,
                  price=px, as_of=T0 - timedelta(minutes=1), valid_until=T0 + valid_for)


def run(env: M2Env, sig: Signal):  # type: ignore[no-untyped-def]
    decision = env.gate.decide(GateRequest.from_signal(sig))
    if decision.approved is not None:
        env.executor.submit(decision.approved)
        env.executor.process_events()
    return decision


def operator_sell(env: M2Env, kind: RequestType, quantity: str, key: str) -> GateRequest:
    return GateRequest(source=RequestSource.OPERATOR, request_type=kind, account=ACCOUNT, instrument="TSTA-USDT",
                       side=Side.SELL, quantity=Decimal(quantity), order_type=OrderType.MARKET,
                       price=RULES["TSTA-USDT"].last, idem_key=key)


def reconcile(env: M2Env) -> str:
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock)) \
        .run(ACCOUNT, "PERIODIC").result


def fill_audits(env: M2Env) -> list[dict]:
    import json
    return [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "FILL"]


# --- 정상 경로: IOC 지정가 매수 → 순 수령 포지션 → 대조 일치 → 시장가 매도 청산 (V-129, D-101, D-104) ----------

def test_entry_net_received_position_then_market_exit(env: M2Env) -> None:
    d = run(env, signal(RequestType.ENTRY))
    assert d.result is GateResult.APPROVED, d.reason
    sent = env.broker.sent[0]
    assert (sent.side, sent.order_type, sent.quantity, sent.price) == ("BUY", OrderType.LIMIT, Decimal("0.1000"),
                                                                       Decimal("10050.00"))
    # 체결 0.1000, 수수료 0.0001 TSTA(기초 자산) → 포지션 = 순 수령 0.0999 (A-062③, D-101)
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == Decimal("0.0999")
    intent = env.repo.intent(d.approved.intent_id)
    assert intent.state is IntentState.FILLED and intent.filled_quantity == Decimal("0.1000")  # 주문 체결은 총 수량
    own = env.repo.active_ownership(ACCOUNT, "TSTA-USDT")
    assert own is not None and own.state == "CONFIRMED" and own.owner == f"{STRATEGY}:1"
    assert env.repo.active_reservations(ACCOUNT) == []
    assert fill_audits(env)[0] == {"quantity": "0.1000", "price": "10050.00",
                                   "commission": "0.0001000", "commission_asset": "TSTA"}
    assert reconcile(env) == "MATCH"  # 브로커 잔고(순 수령)와 내부 포지션 일치, K4 없음 (V-129)
    assert env.repo.halt_rows(ACCOUNT) == []

    # 총 체결 수량으로 매도하면 보유(순 수령) 초과 → G9 거부, 전송 0
    over = run(env, signal(RequestType.EXIT, quantity="0.1000", evaluation="eval-2"))
    assert over.result is GateResult.REJECTED and over.failed_check is Check.G9
    assert len(env.broker.sent) == 1

    out = run(env, signal(RequestType.EXIT, quantity="0.0999", evaluation="eval-3"))
    assert out.result is GateResult.APPROVED, out.reason
    sell = env.broker.sent[1]
    assert (sell.side, sell.order_type, sell.quantity) == ("SELL", OrderType.MARKET, Decimal("0.0999"))
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == Decimal("0")
    assert env.repo.active_ownership(ACCOUNT, "TSTA-USDT") is None
    assert fill_audits(env)[1]["commission_asset"] == "USDT"  # 매도 수수료는 호가 자산 — 포지션에 영향 없음
    assert reconcile(env) == "MATCH"
    kinds = {e["kind"] for e in env.repo.audit_events()}
    assert {"GATE_DECISION", "INTENT_STATE", "ATTEMPT", "BROKER_ORDER", "FILL"} <= kinds


# --- V-01 한도 경계 (소수 수량·USDT 금액): L2 20% = 2,000 USDT, L3 50% = 5,000 USDT -------------------------

@pytest.mark.parametrize(("qty", "approved"), [("0.1999", True), ("0.2000", True), ("0.2001", False)])
def test_v01_symbol_limit_boundary_decimal(env: M2Env, qty: str, approved: bool) -> None:
    d = run(env, signal(RequestType.ENTRY, quantity=qty, price="10000.00"))
    assert (d.result is GateResult.APPROVED) is approved
    if not approved:
        assert d.failed_check is Check.G7 and env.broker.sent == []


def test_v01_total_limit_boundary_decimal(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY  # 예약 노출로 합산
    assert run(env, signal(RequestType.ENTRY, "TSTA-USDT", "0.2000", "10000.00", "e1")).result is GateResult.APPROVED
    assert run(env, signal(RequestType.ENTRY, "TSTB-USDT", "800.0", "2.5000", "e2")).result is GateResult.APPROVED
    over = run(env, signal(RequestType.ENTRY, "TSTC-USDT", "400.1", "2.5000", "e3"))  # 합계 5,000.25 USDT
    assert over.result is GateResult.REJECTED and over.failed_check is Check.G7
    exact = run(env, signal(RequestType.ENTRY, "TSTC-USDT", "400.0", "2.5000", "e4"))  # 합계 5,000.00 USDT
    assert exact.result is GateResult.APPROVED
    assert len(env.broker.sent) == 3


# --- G10 소수 수량 단위(stepSize)·호가 단위(tickSize) — 가상값 -------------------------------------------------

@pytest.mark.parametrize(("qty", "price"), [("0.10005", "10050.00"), ("0.1000", "10050.005")])
def test_g10_step_and_tick_violation_rejected(env: M2Env, qty: str, price: str) -> None:
    d = run(env, signal(RequestType.ENTRY, quantity=qty, price=price))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G10
    assert env.broker.sent == []


# --- V-04 동시 신호 합산 한도 ---------------------------------------------------------------------------------

def test_v04_concurrent_signals_respect_total_limit(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    sigs = [signal(RequestType.ENTRY, "TSTA-USDT", "0.2000", "10000.00", "c0"),
            signal(RequestType.ENTRY, "TSTB-USDT", "800.0", "2.5000", "c1"),
            signal(RequestType.ENTRY, "TSTC-USDT", "800.0", "2.5000", "c2")]  # 각 2,000 USDT, 합 6,000 > 5,000
    results = []
    threads = [threading.Thread(target=lambda s=s: results.append(env.gate.decide(GateRequest.from_signal(s))))
               for s in sigs]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    approved = [d for d in results if d.result is GateResult.APPROVED]
    for d in approved:
        env.executor.submit(d.approved)
    rejected = [d for d in results if d.result is GateResult.REJECTED]
    assert len(approved) == 2 and len(rejected) == 1 and rejected[0].failed_check is Check.G7
    assert sum(Decimal(r.amount) for r in env.repo.active_reservations(ACCOUNT)) == Decimal("4000.0000")
    assert len(env.broker.sent) == 2


# --- V-07 만료, V-12 바꾸지 않음, V-55 중복 --------------------------------------------------------------------

def test_v07_expired_signal_rejected(env: M2Env) -> None:
    d = run(env, signal(RequestType.ENTRY, valid_for=timedelta(seconds=-1)))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G3 and env.broker.sent == []


def test_v12_oversized_signal_rejected_not_resized(env: M2Env) -> None:
    d = run(env, signal(RequestType.ENTRY, quantity="0.2500", price="10000.00"))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G7 and d.approved is None
    assert env.repo.decision(d.decision_id)["quantity"] == "0.2500"
    assert env.repo.intents() == [] and env.broker.sent == []


def test_v55_duplicate_signal_creates_one_intent(env: M2Env) -> None:
    sig = signal(RequestType.ENTRY)
    assert run(env, sig).result is GateResult.APPROVED
    again = env.gate.decide(GateRequest.from_signal(sig))
    assert again.result is GateResult.REJECTED and again.failed_check is Check.G4
    assert len(env.repo.intents()) == 1 and len(env.broker.sent) == 1


# --- V-20 K1: 신규 진입 거부, 운영 요청 감액 허용, 자동 청산 없음 ------------------------------------------------

def test_v20_k1_blocks_entry_allows_operator_reduce(env: M2Env) -> None:
    run(env, signal(RequestType.ENTRY))  # 보유 0.0999 TSTA
    sent_before = len(env.broker.sent)
    env.halts.activate(HaltType.K1, actor="local", reason="test")

    blocked = run(env, signal(RequestType.ENTRY, instrument="TSTB-USDT", quantity="100.0", evaluation="e2"))
    assert blocked.result is GateResult.REJECTED and blocked.failed_check is Check.G2
    assert len(env.broker.sent) == sent_before  # 자동 청산 매도 없음 (정지는 청산이 아님)
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == Decimal("0.0999")

    reduce = env.gate.decide(operator_sell(env, RequestType.REDUCE, "0.0500", "op-reduce"))
    assert reduce.result is GateResult.APPROVED, reduce.reason
    env.executor.submit(reduce.approved)
    env.executor.process_events()
    assert env.broker.sent[-1].quantity == Decimal("0.0500")
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == Decimal("0.0499")


def test_v20_kill_file_blocks_risk_increase_send(env: M2Env) -> None:
    run(env, signal(RequestType.ENTRY))
    d = env.gate.decide(GateRequest.from_signal(signal(RequestType.ENTRY, "TSTB-USDT", "100.0", evaluation="e2")))
    assert d.result is GateResult.APPROVED
    sent_before = len(env.broker.sent)
    env.kill_file.write_text("")
    assert env.executor.submit(d.approved) is IntentState.NOT_SENT
    assert len(env.broker.sent) == sent_before
    assert env.repo.active_reservations(ACCOUNT) == [] and env.repo.active_ownership(ACCOUNT, "TSTB-USDT") is None
    out = run(env, signal(RequestType.EXIT, quantity="0.0999", evaluation="e3"))  # 위험 축소는 전송
    assert out.result is GateResult.APPROVED and len(env.broker.sent) == sent_before + 1


# --- V-26 저장 실패 ------------------------------------------------------------------------------------------

def test_v26_storage_failure_sends_nothing(env: M2Env) -> None:
    env.repo.fail_next_transaction = True
    with pytest.raises(StorageFailure):
        env.gate.decide(GateRequest.from_signal(signal(RequestType.ENTRY)))
    assert env.broker.sent == [] and env.repo.intents() == []


# --- 모드·환경 불일치 차단 (G1, R-01) --------------------------------------------------------------------------

def test_mode_environment_mismatch_blocks_orders(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, broker_env="mock")  # BACKTEST 저장소에 모의 환경 계좌 → 주문 금지
    try:
        d = run(e, signal(RequestType.ENTRY))
        assert d.result is GateResult.REJECTED and d.failed_check is Check.G1
        assert e.broker.sent == []
        with pytest.raises(ModeMismatch):  # 다른 모드 계좌는 저장소에 등록조차 안 됨
            e.repo.register_account(Account(alias="BN-PAPER-01", mode=Mode.PAPER, broker_env="mock"))
    finally:
        e.repo.close()


# --- V-130(최소): 수수료 자산이 기초·USDT 외 → 원장 변경 없이 K4 ----------------------------------------------

def test_unapproved_commission_asset_raises_k4(env: M2Env) -> None:
    env.broker.spot_fees = FakeSpotFees(rate=Decimal("0.001"), pairs=env.broker.spot_fees.pairs,
                                        asset_override="TSTFEE")  # 가상 제3 자산 (BNB 납부 경로 모사)
    d = run(env, signal(RequestType.ENTRY))
    assert d.result is GateResult.APPROVED
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == Decimal("0")
    assert [r["halt_type"] for r in env.repo.halt_rows(ACCOUNT)] == ["K4"]
    nxt = run(env, signal(RequestType.ENTRY, instrument="TSTB-USDT", quantity="100.0", evaluation="e2"))
    assert nxt.result is GateResult.REJECTED and nxt.failed_check is Check.G2
