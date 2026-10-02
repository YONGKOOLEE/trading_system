"""T-001 최소 종단 간 흐름 (05-plan §3).

가상 입력 → 신호 → 게이트 승인/거부 → fake 브로커 → 기록, 최소 킬 스위치.
검증: V-01(L2·L3 경계), V-04, V-07, V-12, V-20(K1 부분·KILL 파일), V-26, V-55, G6·G9.
모든 테스트 종료 시 conftest가 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import sqlite3
import threading
from datetime import timedelta
from decimal import Decimal

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import Check, GateResult, HaltType, IntentState, OrderType, RequestSource, RequestType, Side
from trading_system.domain.models import GateRequest
from trading_system.storage.repository import StorageFailure


def _enter(env: Env, **kw) -> tuple:
    decision = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, **kw)))
    state = env.executor.submit(decision.approved) if decision.approved else None
    env.executor.process_events()
    return decision, state


# --- 정상 경로 -------------------------------------------------------------

def test_entry_then_exit_records_everything(env: Env) -> None:
    decision, state = _enter(env)
    assert decision.result is GateResult.APPROVED
    assert state is IntentState.SENT or state is IntentState.ACCEPTED
    pos = env.repo.position(ACCOUNT, "TST-A")
    assert pos.quantity == Decimal("100")
    own = env.repo.active_ownership(ACCOUNT, "TST-A")
    assert own is not None and own.state == "CONFIRMED" and own.owner == "t001-fixed:1"
    assert env.repo.active_reservations(ACCOUNT) == []
    assert env.repo.intent(decision.approved.intent_id).state is IntentState.FILLED

    exit_decision = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.EXIT, evaluation="eval-2")))
    assert exit_decision.result is GateResult.APPROVED
    env.executor.submit(exit_decision.approved)
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("0")
    assert env.repo.active_ownership(ACCOUNT, "TST-A") is None
    kinds = [e["kind"] for e in env.repo.audit_events()]
    for expected in ("GATE_DECISION", "INTENT_STATE", "ATTEMPT", "BROKER_ORDER", "FILL"):
        assert expected in kinds
    assert len(env.broker.sent) == 2


# --- V-01 한도 경계 (L2 20% = 2,000,000원, L3 50% = 5,000,000원) ------------

@pytest.mark.parametrize(("qty", "approved"), [("199", True), ("200", True), ("201", False)])
def test_v01_symbol_limit_boundary(env: Env, qty: str, approved: bool) -> None:
    decision, _ = _enter(env, quantity=qty)
    assert (decision.result is GateResult.APPROVED) is approved
    if not approved:
        assert decision.failed_check is Check.G7
        assert env.broker.sent == []


def test_v01_total_limit_boundary(env: Env) -> None:
    assert _enter(env, instrument="TST-A", quantity="200", evaluation="e1")[0].result is GateResult.APPROVED
    assert _enter(env, instrument="TST-B", quantity="200", evaluation="e2")[0].result is GateResult.APPROVED
    over, _ = _enter(env, instrument="TST-C", quantity="101", evaluation="e3")  # 합계 5,010,000원
    assert over.result is GateResult.REJECTED and over.failed_check is Check.G7
    exact, _ = _enter(env, instrument="TST-C", quantity="100", evaluation="e4")  # 합계 5,000,000원
    assert exact.result is GateResult.APPROVED


# --- V-04 동시 신호 합산 한도 ----------------------------------------------

def test_v04_concurrent_signals_respect_total_limit(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY  # 체결 전 예약 노출 상태에서 합산되게 함
    # 각 2,000,000원: 종목 한도(L2) 경계에서 개별 통과, 셋을 합치면 6,000,000원 > L3 5,000,000원
    signals = [
        make_signal(RequestType.ENTRY, instrument=inst, quantity="200", evaluation=f"c{i}")
        for i, inst in enumerate(["TST-A", "TST-B", "TST-C"])
    ]
    results = []

    def run(sig) -> None:
        results.append(env.gate.decide(GateRequest.from_signal(sig)))

    threads = [threading.Thread(target=run, args=(s,)) for s in signals]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    approved = [d for d in results if d.result is GateResult.APPROVED]
    for d in approved:
        env.executor.submit(d.approved)
    rejected = [d for d in results if d.result is GateResult.REJECTED]
    total = sum(Decimal(r.amount) for r in env.repo.active_reservations(ACCOUNT))
    assert total == Decimal("4000000") and total <= Decimal("5000000")
    assert len(approved) == 2  # 판정 순서와 무관하게 셋 중 하나는 합산 한도로 거부
    assert len(rejected) == 1 and rejected[0].failed_check is Check.G7
    assert len(env.broker.sent) == 2


# --- V-07 기한 만료 ---------------------------------------------------------

def test_v07_expired_signal_rejected(env: Env) -> None:
    sig = make_signal(RequestType.ENTRY, valid_for=timedelta(minutes=-1))
    decision = env.gate.decide(GateRequest.from_signal(sig))
    assert decision.result is GateResult.REJECTED and decision.failed_check is Check.G3
    assert env.broker.sent == []


# --- V-12 게이트는 요청을 바꾸지 않는다 ------------------------------------

def test_v12_oversized_signal_rejected_not_resized(env: Env) -> None:
    decision, _ = _enter(env, quantity="500")
    assert decision.result is GateResult.REJECTED and decision.failed_check is Check.G7
    assert decision.approved is None
    assert env.repo.decision(decision.decision_id)["quantity"] == "500"
    assert env.repo.intents() == []
    assert env.broker.sent == []


# --- V-55 중복 신호 ---------------------------------------------------------

def test_v55_duplicate_signal_creates_one_intent(env: Env) -> None:
    sig = make_signal(RequestType.ENTRY)
    first = env.gate.decide(GateRequest.from_signal(sig))
    env.executor.submit(first.approved)
    second = env.gate.decide(GateRequest.from_signal(sig))
    assert second.result is GateResult.REJECTED and second.failed_check is Check.G4
    assert len(env.repo.intents()) == 1
    assert len(env.broker.sent) == 1


def test_v55_rejected_signal_redelivered_stays_rejected(env: Env) -> None:
    """01 D-016: 거부된 신호는 다시 판정하지 않는다. 한도가 비어도 같은 신호는 중복으로 거부."""
    _enter(env, instrument="TST-A", quantity="200", evaluation="e1")
    _enter(env, instrument="TST-B", quantity="200", evaluation="e2")
    sig = make_signal(RequestType.ENTRY, instrument="TST-C", quantity="200", evaluation="e3")
    first = env.gate.decide(GateRequest.from_signal(sig))
    assert first.failed_check is Check.G7  # 합계 6,000,000원 > 5,000,000원
    exit_d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.EXIT, instrument="TST-B",
                                                                 quantity="200", evaluation="e4")))
    env.executor.submit(exit_d.approved)
    env.executor.process_events()  # 한도 여유가 생김
    again = env.gate.decide(GateRequest.from_signal(sig))
    assert again.result is GateResult.REJECTED and again.failed_check is Check.G4
    assert not any(r.instrument == "TST-C" for r in env.broker.sent)


# --- V-20 K1 킬 스위치 (부분) ------------------------------------------------

def test_v20_k1_blocks_entry_allows_exit_and_cancels_open_buys(env: Env) -> None:
    _enter(env, instrument="TST-A")  # 보유 100주
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    open_buy, _ = _enter(env, instrument="TST-B", evaluation="e2")  # 미체결 매수
    sent_before = len(env.broker.sent)

    env.halts.activate(HaltType.K1, actor="local", reason="test")

    blocked = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-C", evaluation="e3")))
    assert blocked.result is GateResult.REJECTED and blocked.failed_check is Check.G2
    # 정지 정책이 미체결 매수 취소 요청을 게이트를 거쳐 보냄
    cancels = [r for r in env.broker.sent[sent_before:] if r.kind == "CANCEL"]
    assert len(cancels) == 1 and cancels[0].instrument == "TST-B"
    env.executor.process_events()
    assert env.repo.intent(open_buy.approved.intent_id).state is IntentState.CANCELLED
    assert env.repo.active_ownership(ACCOUNT, "TST-B") is None
    # 보유분은 자동 청산하지 않음
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("100")
    assert not any(r.kind == "NEW" and r.side == "SELL" for r in env.broker.sent)
    # 청산: 01 §2.6 K1은 운영 요청의 감액·청산·취소만 허용 (T-005에서 표대로 정밀화, CP-017 T5-1)
    env.broker.behavior = FakeBehavior.FILL
    sig_exit = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.EXIT, instrument="TST-A", evaluation="e4")))
    assert sig_exit.result is GateResult.REJECTED and sig_exit.failed_check is Check.G2
    exit_d = env.gate.decide(GateRequest(source=RequestSource.OPERATOR, request_type=RequestType.EXIT, account=ACCOUNT,
                                         instrument="TST-A", side=Side.SELL, quantity=Decimal("100"),
                                         order_type=OrderType.MARKET, price=Decimal("10000"), idem_key="op-exit"))
    assert exit_d.result is GateResult.APPROVED
    env.executor.submit(exit_d.approved)
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("0")


def test_v20_kill_file_blocks_risk_increase_send(env: Env) -> None:
    _enter(env, instrument="TST-A")  # 보유 100주
    decision = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-B", evaluation="e2")))
    assert decision.result is GateResult.APPROVED
    sent_before = len(env.broker.sent)
    env.kill_file.write_text("")
    state = env.executor.submit(decision.approved)
    assert state is IntentState.NOT_SENT
    assert len(env.broker.sent) == sent_before
    assert env.repo.active_reservations(ACCOUNT) == []
    assert env.repo.active_ownership(ACCOUNT, "TST-B") is None
    # 위험 축소는 KILL 파일이 있어도 전송
    exit_d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.EXIT, instrument="TST-A", evaluation="e3")))
    env.executor.submit(exit_d.approved)
    assert len(env.broker.sent) == sent_before + 1


# --- V-26 저장 실패 ----------------------------------------------------------

def test_v26_storage_failure_sends_nothing(env: Env) -> None:
    env.repo.fail_next_transaction = True
    with pytest.raises(StorageFailure):
        env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
    assert env.broker.sent == []
    assert env.repo.intents() == []


# --- G6 소유권, G9 매도 수량, 브로커 거부 -----------------------------------

def test_g6_other_strategy_cannot_enter_owned_symbol(env: Env) -> None:
    _enter(env, instrument="TST-A")
    other, _ = _enter(env, instrument="TST-A", strategy="other", evaluation="e2", quantity="10")
    assert other.result is GateResult.REJECTED and other.failed_check is Check.G6


def test_g9_sell_more_than_position_rejected(env: Env) -> None:
    _enter(env, instrument="TST-A")
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.REDUCE, quantity="150", evaluation="e2")))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G9


def test_broker_reject_releases_without_resend(env: Env) -> None:
    env.broker.behavior = FakeBehavior.REJECT
    decision, state = _enter(env)
    assert state is IntentState.REJECTED
    assert len(env.broker.sent) == 1
    assert env.repo.active_reservations(ACCOUNT) == []
    assert env.repo.active_ownership(ACCOUNT, "TST-A") is None


def test_audit_is_append_only(env: Env) -> None:
    _enter(env)
    with pytest.raises(sqlite3.DatabaseError):
        env.repo.connection.execute("UPDATE audit_event SET kind = 'X' WHERE seq = 1")
    with pytest.raises(sqlite3.DatabaseError):
        env.repo.connection.execute("DELETE FROM audit_event")
