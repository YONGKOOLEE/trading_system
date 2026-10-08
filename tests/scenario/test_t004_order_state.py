"""T-004 주문 상태 기계·결과 불명 (05-plan T-004, 04 §3.3·§3.4, 02 ST-01~05, 03 D-047).

검증: V-40~V-48, V-58, V-59 + 취소·정정의 결과 불명 해소.
모든 테스트 종료 시 conftest가 불변 조건(INV-02·11 포함)을 검사한다.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import (
    Check, GateResult, IntentState, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.models import GateDecision, GateRequest
from trading_system.execution.executor import Executor
from trading_system.execution.resolver import ResolveOutcome, UnknownResolver


def resolver(env: Env, waits: list[float]) -> UnknownResolver:
    def wait(seconds: float) -> None:
        waits.append(seconds)
        env.clock.advance(timedelta(seconds=seconds))
    return UnknownResolver(env.repo, env.broker, env.executor, env.clock, wait)


def enter(env: Env, inst: str = "TST-A", qty: str = "100", ev: str = "e1") -> GateDecision:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument=inst, quantity=qty,
                                                            evaluation=ev)))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    return d


def state(env: Env, d: GateDecision) -> IntentState:
    return env.repo.intent(d.approved.intent_id).state


def alerts(env: Env, level: str) -> list[dict]:
    import json
    return [json.loads(e["summary"]) for e in env.repo.audit_events()
            if e["kind"] == "ALERT" and json.loads(e["summary"])["level"] == level]


# --- V-40 응답 유실 → 조회로 발견 ---------------------------------------------------

def test_v40_lost_response_found_by_query_no_resend(env: Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    d = enter(env)
    assert state(env, d) is IntentState.UNKNOWN
    assert alerts(env, "EMERGENCY")
    waits: list[float] = []
    assert resolver(env, waits).resolve(d.approved.intent_id) is ResolveOutcome.FOUND
    assert waits == [2]
    assert state(env, d) is IntentState.ACCEPTED
    assert len(env.broker.sent) == 1  # 재전송 없음
    assert env.repo.active_reservations(ACCOUNT)  # 미체결 매수 예약 유지


def test_v40_lost_response_with_fill_reflects_fills_once(env: Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_FILL
    d = enter(env)  # 실시간 체결 통보는 주문번호를 몰라 짝 없음으로 기록됨
    assert state(env, d) is IntentState.UNKNOWN
    assert resolver(env, []).resolve(d.approved.intent_id) is ResolveOutcome.FOUND
    assert state(env, d) is IntentState.FILLED
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("100")
    own = env.repo.active_ownership(ACCOUNT, "TST-A")
    assert own is not None and own.state == "CONFIRMED"
    # 같은 체결 통보가 다시 와도 중복 반영 없음
    from trading_system.domain.models import OrderEvent
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.inject(OrderEvent(kind="FILL", order_no=order.order_no, fill_id="F0000001",
                                 quantity=Decimal("100"), price=Decimal("10000")))
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("100")


def test_v40_delayed_visibility_found_on_later_round(env: Env) -> None:
    env.broker.query_delay = 4  # 조회 2라운드(주문·체결 조회 4회) 뒤에야 보임
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    d = enter(env)
    waits: list[float] = []
    assert resolver(env, waits).resolve(d.approved.intent_id) is ResolveOutcome.FOUND
    assert waits == [2, 4, 8]


# --- V-41 응답 유실 + 브로커도 받지 않음 → 5회 조회 후 K3·사람 확인 (자동 미접수 종결 없음) ----------------------

def test_v41_not_received_escalates_k3_after_five_queries(env: Env) -> None:
    # 요구사항 변경 A-084/CP-031: 5회 모두 주문 없음 → NOT_ACCEPTED 자동 종결 대신 UNKNOWN 유지 + K3 (D-047 M1 → D-111 규칙)
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = enter(env)
    waits: list[float] = []
    assert resolver(env, waits).resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS
    assert waits == [2, 4, 8, 16, 32]                       # 회차·간격 변경 없음
    assert state(env, d) is IntentState.UNKNOWN
    assert env.repo.active_reservations(ACCOUNT) != []      # 예약 유지 → 재진입 불가
    assert env.repo.active_ownership(ACCOUNT, "TST-A") is not None
    assert len(env.broker.sent) == 1
    assert env.repo.active_halts(ACCOUNT) == ["K3"]


# --- V-42 조회 모호 → K3 ------------------------------------------------------------

def test_v42_query_failure_mixed_goes_k3(env: Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = enter(env)
    env.broker.query_failures = 1
    assert resolver(env, []).resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS
    assert state(env, d) is IntentState.UNKNOWN  # 결과 불명 유지
    assert "K3" in env.repo.active_halts(ACCOUNT)
    assert any("K3" in a["message"] for a in alerts(env, "EMERGENCY"))
    env.broker.behavior = FakeBehavior.FILL
    other = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-B",
                                                                evaluation="e2")))
    assert other.result is GateResult.REJECTED and other.failed_check is Check.G2  # 계좌 전체 위험 확대 차단


def test_v42_two_candidates_goes_k3(env: Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    d = enter(env)
    env.broker.add_external_order(ACCOUNT, "TST-A", "BUY", Decimal("100"))  # 같은 조건의 외부 주문
    assert resolver(env, []).resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS
    assert "K3" in env.repo.active_halts(ACCOUNT)


def test_v42_partial_match_goes_k3(env: Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = enter(env)
    env.broker.add_external_order(ACCOUNT, "TST-A", "BUY", Decimal("70"))  # 종목·방향 같고 수량 다름
    assert resolver(env, []).resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS


def test_v42_unmatched_realtime_event_prevents_not_accepted(env: Env) -> None:
    from trading_system.domain.models import OrderEvent
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = enter(env)
    env.broker.inject(OrderEvent(kind="FILL", order_no="9999999", fill_id="X1", quantity=Decimal("1"),
                                 price=Decimal("10000")))
    env.executor.process_events()
    assert resolver(env, []).resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS


# --- V-43 결과 불명 중 새 신호 ------------------------------------------------------------

def test_v43_new_signal_on_unknown_symbol_rejected(env: Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    enter(env)
    env.broker.behavior = FakeBehavior.FILL
    again = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, evaluation="e2")))
    assert again.result is GateResult.REJECTED and again.failed_check in (Check.G4, Check.G5)
    assert (Check.G5, False) in again.checks
    assert len(env.broker.sent) == 1


# --- V-44·V-45 명시 거부, 연속 거부 알림 ------------------------------------------------

def test_v44_explicit_reject_releases_without_resend(env: Env) -> None:
    env.broker.behavior = FakeBehavior.REJECT
    d = enter(env)
    assert state(env, d) is IntentState.REJECTED
    assert env.repo.active_reservations(ACCOUNT) == [] and env.repo.active_ownership(ACCOUNT, "TST-A") is None
    assert len(env.broker.sent) == 1


def test_v45_three_same_rejects_raise_warning(env: Env) -> None:
    env.broker.behavior = FakeBehavior.REJECT
    enter(env, "TST-A", ev="r1")
    enter(env, "TST-B", ev="r2")
    assert alerts(env, "WARNING") == []
    enter(env, "TST-C", ev="r3")
    [w] = alerts(env, "WARNING")
    assert "연속 거부 3회" in w["message"]


# --- V-46 부분 체결 후 마감 ------------------------------------------------------------------

def test_v46_partial_fill_then_close_expires_remainder(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = enter(env)
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no, Decimal("30"))
    env.executor.process_events()
    assert state(env, d) is IntentState.PARTIAL
    env.broker.expire_open()
    env.executor.process_events()
    i = env.repo.intent(d.approved.intent_id)
    assert i.state is IntentState.EXPIRED and i.filled_quantity == Decimal("30")
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("30")
    own = env.repo.active_ownership(ACCOUNT, "TST-A")
    assert own is not None and own.state == "CONFIRMED"
    assert env.repo.active_reservations(ACCOUNT) == []


# --- V-47 전송 전 실패 -------------------------------------------------------------------------

def test_v47_pre_send_failure_closes_without_resend(env: Env) -> None:
    env.broker.behavior = FakeBehavior.PRE_SEND_FAIL
    d = enter(env)
    assert state(env, d) is IntentState.NOT_SENT
    assert env.broker.sent == []
    assert env.repo.attempt_for(d.approved.intent_id)["state"] == "NOT_SENT"
    assert env.repo.active_reservations(ACCOUNT) == []


# --- V-48 정정·취소는 새 주문번호 + 원주문 참조 ----------------------------------------------

def _open_limit_buy(env: Env) -> tuple[GateDecision, str]:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    s = make_signal(RequestType.ENTRY)
    req = GateRequest(source=RequestSource.SIGNAL, request_type=RequestType.ENTRY, account=ACCOUNT,
                      instrument="TST-A", side=Side.BUY, quantity=Decimal("100"), order_type=OrderType.LIMIT,
                      price=Decimal("9900"), idem_key=s.signal_id, owner=s.owner, valid_until=s.valid_until,
                      signal_id=s.signal_id)
    d = env.gate.decide(req)
    env.executor.submit(d.approved)
    return d, env.repo.intent(d.approved.intent_id).broker_order_key


def _modify(key: str, qty: str, price: str, idem: str = "m-1") -> GateRequest:
    return GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY, account=ACCOUNT,
                       instrument="TST-A", side=Side.BUY, quantity=Decimal(qty), order_type=OrderType.LIMIT,
                       price=Decimal(price), idem_key=idem, owner="t001-fixed:1", target_order_key=key)


def test_v48_modify_gets_new_order_number_and_references_original(env: Env) -> None:
    entry, key = _open_limit_buy(env)
    m = env.gate.decide(_modify(key, "100", "9950"))
    assert m.result is GateResult.APPROVED
    assert env.executor.submit(m.approved) is IntentState.ACCEPTED
    sent = env.broker.sent[-1]
    assert sent.kind == "MODIFY" and sent.target_order_no == env.repo.broker_order(key).order_no
    new_key = env.repo.intent(m.approved.intent_id).broker_order_key
    new = env.repo.broker_order(new_key)
    assert new.order_no != env.repo.broker_order(key).order_no
    env.executor.process_events()  # 정정 확인
    assert env.repo.broker_order(key).state == "MODIFIED"
    assert state(env, m) is IntentState.DONE
    i = env.repo.intent(entry.approved.intent_id)
    assert i.quantity == Decimal("100") and i.price == Decimal("9950") and not i.state.is_terminal
    env.broker.fill(new.order_no)  # 새 주문 체결은 원 의도에 반영
    env.executor.process_events()
    assert env.repo.intent(entry.approved.intent_id).state is IntentState.FILLED
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("100")
    assert env.repo.active_reservations(ACCOUNT) == []


def test_v48_cancel_gets_new_order_number_and_references_original(env: Env) -> None:
    entry, key = _open_limit_buy(env)
    c = env.gate.decide(GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, "TST-A", Side.BUY, key,
                                           idem_key="c-1", owner=None))
    env.executor.submit(c.approved)
    cancel_order = env.repo.broker_order(env.repo.intent(c.approved.intent_id).broker_order_key)
    assert cancel_order.order_no != env.repo.broker_order(key).order_no
    row = env.repo.connection.execute("SELECT origin, target_order_key FROM broker_order WHERE order_key=?",
                                      (cancel_order.order_key,)).fetchone()
    assert tuple(row) == ("INTERNAL_CANCEL", key)
    env.executor.process_events()
    assert state(env, entry) is IntentState.CANCELLED and state(env, c) is IntentState.DONE


def test_cancel_with_lost_response_resolved_by_query(env: Env) -> None:
    entry, key = _open_limit_buy(env)
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    c = env.gate.decide(GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, "TST-A", Side.BUY, key,
                                           idem_key="c-1", owner=None))
    env.executor.submit(c.approved)
    env.executor.process_events()  # 취소 확인 통보는 대상 주문(내부에 있음)으로 반영됨
    assert state(env, c) is IntentState.UNKNOWN
    assert resolver(env, []).resolve(c.approved.intent_id) is ResolveOutcome.FOUND
    assert state(env, c) is IntentState.DONE and state(env, entry) is IntentState.CANCELLED
    assert env.repo.active_reservations(ACCOUNT) == []


def test_modify_with_lost_response_resolved_by_query(env: Env) -> None:
    entry, key = _open_limit_buy(env)
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    m = env.gate.decide(_modify(key, "100", "9950"))
    env.executor.submit(m.approved)
    env.executor.process_events()  # 정정 확인 통보가 오지만 새 주문을 아직 몰라 보류 기록
    assert state(env, m) is IntentState.UNKNOWN
    assert resolver(env, []).resolve(m.approved.intent_id) is ResolveOutcome.FOUND
    assert state(env, m) is IntentState.DONE
    assert env.repo.broker_order(key).state == "MODIFIED"
    assert env.repo.intent(entry.approved.intent_id).quantity == Decimal("100")


def test_modify_rejected_keeps_original(env: Env) -> None:
    entry, key = _open_limit_buy(env)
    env.broker.behavior = FakeBehavior.REJECT
    m = env.gate.decide(_modify(key, "100", "9995"))
    env.executor.submit(m.approved)
    assert state(env, m) is IntentState.REJECTED
    assert env.repo.broker_order(key).state == "ACCEPTED"
    assert env.repo.intent(entry.approved.intent_id).quantity == Decimal("100")


# --- V-58·V-59 재시작 ---------------------------------------------------------------------------

def test_v58_restart_with_awaiting_attempt_becomes_unknown_and_is_queried(env: Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
    intent = env.repo.intent(d.approved.intent_id)
    # 전송 시도를 기록하고 실제로 브로커가 받은 직후 프로세스가 죽은 상황
    with env.repo.tx():
        env.repo.insert_attempt(intent.intent_id, env.clock.now())
        env.repo.set_intent_state(intent.intent_id, IntentState.SENT)
    from trading_system.domain.models import OrderRequest
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    env.broker.place_order(OrderRequest("NEW", ACCOUNT, "TST-A", "BUY", Decimal("100"), OrderType.MARKET,
                                        Decimal("10000")))
    restarted = Executor(env.repo, env.broker, env.clock, env.kill_file)
    unknown = restarted.recover_on_start()
    assert unknown == [intent.intent_id]
    assert env.repo.intent(intent.intent_id).state is IntentState.UNKNOWN
    r = UnknownResolver(env.repo, env.broker, restarted, env.clock, lambda s: None)
    assert r.resolve(intent.intent_id) is ResolveOutcome.FOUND
    assert len(env.broker.sent) == 1


def test_v59_restart_closes_pending_send_without_sending(env: Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
    restarted = Executor(env.repo, env.broker, env.clock, env.kill_file)
    assert restarted.recover_on_start() == []
    assert env.repo.intent(d.approved.intent_id).state is IntentState.NOT_SENT
    assert env.broker.sent == []
    assert env.repo.active_reservations(ACCOUNT) == [] and env.repo.active_ownership(ACCOUNT, "TST-A") is None
    import pytest
    with pytest.raises(ValueError):
        restarted.submit(d.approved)  # 종결된 의도는 다시 보내지 않음


def test_modify_up_moves_extra_reservation_to_original_intent(env: Env) -> None:
    entry, key = _open_limit_buy(env)  # 100주 × 9,900 = 990,000 예약
    m = env.gate.decide(_modify(key, "100", "9950"))
    assert env.repo.reservation_for(m.approved.intent_id).amount == Decimal("5000")
    env.executor.submit(m.approved)
    env.executor.process_events()
    [rsv] = env.repo.active_reservations(ACCOUNT)
    assert rsv.intent_id == entry.approved.intent_id and rsv.amount == Decimal("995000")
