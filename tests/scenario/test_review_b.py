"""재검토(2026-10-02 2차, CP-018)에서 재현한 결함의 회귀 시험. 모든 값은 가상이다.

R-1  주문번호를 거래일 없이 조회 → 미접수 오판·다른 날 체결 오반영 (03 D-046)
R-2  접수 응답보다 체결 통보가 먼저 오면 체결 유실
R-3  승인 후 전송 전에 정지 발동·신호 기한 초과 → 그대로 전송 (02 ST-01)
R-4  운영 명령 하나의 오류가 명령 큐 전체를 막음
R-5  kill 스크립트가 현재 폴더 기준 경로를 씀
R-6  증액 미체결 중 청산 체결 → 소유권 해제, 고아 포지션 (R-28)
R-7  K10 해제가 종목 범위를 무시
R-8  소유자를 비운 정정이 K9를 우회
R-9  정정 후 부분 체결에서 예약이 적게 잡힘
R-10 정지 정책 취소가 한 번 거부되면 다시 시도되지 않음
R-11 resume --clear-kill-file이 재개 전에 KILL 파일을 지움
A-*  구조 보강: 전송은 트랜잭션 밖에서만, 결과 불명 조회 중 통보 반영, 스키마 버전, 정정 주문 경로 검사
"""

from __future__ import annotations

import subprocess
import sys
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from trading_system.broker.fake.broker import FakeBehavior, FakeBroker
from trading_system.domain.enums import (
    Check, GateResult, HaltType, IntentState, Mode, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.models import GateRequest, OrderEvent, Signal
from trading_system.execution.executor import Executor
from trading_system.execution.resolver import ResolveOutcome, UnknownResolver
from trading_system.halt.manager import Channel
from trading_system.ops.commands import OpsProcessor
from trading_system.storage.repository import Repository

ROOT = Path(__file__).resolve().parents[2]


def sig(env: Env, kind: RequestType, inst: str = "TST-A", qty: str = "100", ev: str = "e1",
        order_type: OrderType = OrderType.MARKET, price: str = "10000",
        valid: timedelta = timedelta(minutes=30)) -> GateRequest:
    now = env.clock.now()
    return GateRequest.from_signal(Signal("t001-fixed", 1, ev, ACCOUNT, inst, "R1", kind, Decimal(qty), order_type,
                                          Decimal(price), now - timedelta(minutes=1), now + valid))


def run(env: Env, req: GateRequest):
    d = env.gate.decide(req)
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


def resolver(env: Env) -> UnknownResolver:
    return UnknownResolver(env.repo, env.broker, env.executor, env.clock,
                           lambda s: env.clock.advance(timedelta(seconds=s)))


# --- R-1 주문번호의 거래일 범위 ---------------------------------------------------------------

def test_r1_order_number_reused_next_day_is_not_treated_as_known(env: Env) -> None:
    run(env, sig(env, RequestType.ENTRY, "TST-A", "10", ev="d1"))       # 1일차 주문 0000001 체결
    env.clock.advance(timedelta(days=1))
    env.broker.new_day()                                                # 브로커 번호가 날마다 다시 시작
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    d = run(env, sig(env, RequestType.ENTRY, "TST-B", "10", ev="d2"))   # 2일차 0000001, 응답 유실
    assert resolver(env).resolve(d.approved.intent_id) is ResolveOutcome.FOUND
    assert env.repo.intent(d.approved.intent_id).state is IntentState.ACCEPTED
    env.broker.behavior = FakeBehavior.FILL
    again = env.gate.decide(sig(env, RequestType.ENTRY, "TST-B", "10", ev="d3"))
    assert again.result is GateResult.REJECTED  # 이미 접수된 진입이 있으므로 다시 승인하지 않음


def test_r1_next_day_event_does_not_hit_previous_day_order(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(env, RequestType.ENTRY, "TST-A", "100", ev="d1"))  # 1일차 0000001 미체결
    env.broker.expire_open()
    env.broker.drop_events()                                            # 마감 소멸 통보 유실
    env.clock.advance(timedelta(days=1))
    env.broker.new_day()
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("100"))  # 2일차 0000001 = 다른 종목
    env.broker.fill(no)
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == 0
    assert env.repo.intent(d.approved.intent_id).state is IntentState.ACCEPTED  # 어제 의도에 반영하지 않음
    kinds = [e["kind"] for e in env.repo.audit_events()]
    assert "UNMATCHED_EVENT" in kinds


def test_r1_human_confirm_next_day_keys_order_by_send_date(env: Env) -> None:
    """결과 불명을 다음 날 사람이 확정해도 브로커 주문의 거래일은 전송한 날이다 (03 D-046)."""
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    d = run(env, sig(env, RequestType.ENTRY))
    sent_day = env.clock.now()
    env.clock.advance(timedelta(days=1))
    env.halts.confirm_unknown(d.approved.intent_id, True, "user", "0000001")
    order = env.repo.broker_order(env.repo.intent(d.approved.intent_id).broker_order_key)
    assert order.order_key == Repository.order_key(ACCOUNT, sent_day, "0000001")


# --- R-2 접수 응답보다 먼저 온 체결 통보 ------------------------------------------------------

def test_r2_fill_event_before_accept_response_is_applied_after_adoption(env: Env) -> None:
    holder: dict = {}

    class RacyBroker(FakeBroker):
        def place_order(self, req):  # type: ignore[no-untyped-def]
            r = super().place_order(req)
            holder["ex"].process_events()  # 통보 처리가 응답 반영보다 먼저 돎
            return r

    broker = RacyBroker(FakeBehavior.FILL)
    ex = Executor(env.repo, broker, env.clock, env.kill_file)
    holder["ex"] = ex
    d = env.gate.decide(sig(env, RequestType.ENTRY))
    ex.submit(d.approved)
    ex.process_events()
    assert env.repo.intent(d.approved.intent_id).state is IntentState.FILLED
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("100")
    assert env.repo.active_reservations(ACCOUNT) == []


# --- R-3 전송 직전 재확인 --------------------------------------------------------------------

def test_r3_halt_after_approval_blocks_send(env: Env) -> None:
    d = env.gate.decide(sig(env, RequestType.ENTRY))
    env.halts.activate(HaltType.K1, "remote", "원격 킬 (KILL 파일 없음)")
    assert env.executor.submit(d.approved) is IntentState.NOT_SENT
    assert env.broker.sent == []
    assert env.repo.active_reservations(ACCOUNT) == []


def test_r3_expired_signal_after_approval_not_sent(env: Env) -> None:
    d = env.gate.decide(sig(env, RequestType.ENTRY, valid=timedelta(seconds=10)))
    env.clock.advance(timedelta(minutes=5))
    assert env.executor.submit(d.approved) is IntentState.NOT_SENT
    assert env.broker.sent == []


def test_r3_risk_reducing_still_sent_when_halt_allows(env: Env) -> None:
    run(env, sig(env, RequestType.ENTRY, "TST-A", "10"))
    d = env.gate.decide(GateRequest(source=RequestSource.OPERATOR, request_type=RequestType.EXIT, account=ACCOUNT,
                                    instrument="TST-A", side=Side.SELL, quantity=Decimal("10"),
                                    order_type=OrderType.MARKET, price=Decimal("10000"), idem_key="op-x"))
    env.halts.activate(HaltType.K1, "remote", "킬")
    assert env.executor.submit(d.approved) is not IntentState.NOT_SENT  # K1은 운영 요청 청산 허용


# --- R-4 운영 명령 큐 ------------------------------------------------------------------------

def _cmd(env: Env, cid: str, name: str, args: dict, channel: str = Channel.LOCAL) -> None:
    with env.repo.tx():
        env.repo.insert_command(cid, channel, "u", name, args, env.clock.now())


def test_r4_bad_command_does_not_block_kill(env: Env) -> None:
    _cmd(env, "c1", "CANCEL", {"order_key": ["x"]}, Channel.REMOTE)
    _cmd(env, "c2", "KILL", {})
    outs = OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock).process_pending()
    statuses = {o.command_id: o.status for o in outs}
    assert statuses["c2"] == "DONE" and statuses["c1"] in ("REJECTED", "FAILED")
    assert env.repo.active_halts(ACCOUNT) == ["K1"]
    assert env.repo.pending_commands() == []


def test_r4_kill_processed_before_other_commands(env: Env) -> None:
    run(env, sig(env, RequestType.ENTRY, "TST-A", "10"))
    _cmd(env, "c1", "EXIT", {"account": ACCOUNT, "instrument": "TST-A", "quantity": "10"})
    _cmd(env, "c2", "KILL", {})
    outs = OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock).process_pending()
    assert [o.command_id for o in outs] == ["c2", "c1"]


def test_r4_confirm_unknown_accepted_must_be_boolean(env: Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = run(env, sig(env, RequestType.ENTRY))
    _cmd(env, "c1", "CONFIRM_UNKNOWN", {"intent_id": d.approved.intent_id, "accepted": "true", "order_no": "0000009"})
    [o] = OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock).process_pending()
    assert o.status == "REJECTED"
    assert env.repo.intent(d.approved.intent_id).state is IntentState.UNKNOWN


# --- R-5 kill 스크립트 경로 -------------------------------------------------------------------

def test_r5_kill_script_defaults_to_repository_root_not_cwd(tmp_path: Path) -> None:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "status.py"), "--mode", "BACKTEST"],
                          capture_output=True, text=True, timeout=60, cwd=tmp_path)
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert str(ROOT / "var" / "backtest") in proc.stdout
    assert not (tmp_path / "var").exists()


# --- R-6 소유권 ------------------------------------------------------------------------------

def test_r6_exit_fill_with_pending_increase_keeps_ownership(env: Env) -> None:
    run(env, sig(env, RequestType.ENTRY, "TST-A", "100", ev="a"))
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    inc = run(env, sig(env, RequestType.INCREASE, "TST-A", "50", ev="b"))
    env.broker.behavior = FakeBehavior.FILL
    run(env, sig(env, RequestType.EXIT, "TST-A", "100", ev="c"))
    order = env.repo.broker_order(env.repo.intent(inc.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no)
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("50")
    own = env.repo.active_ownership(ACCOUNT, "TST-A")
    assert own is not None and own.owner == "t001-fixed:1"


# --- R-7 K10 범위 ----------------------------------------------------------------------------

def test_r7_k10_release_respects_instrument_scope(env: Env) -> None:
    env.halts.activate(HaltType.K10, "monitor", "품질", ACCOUNT, instrument="TST-A")
    env.halts.activate(HaltType.K10, "monitor", "품질", ACCOUNT, instrument="TST-B")
    released = env.halts.report_condition(HaltType.K10, ACCOUNT, "data_ok", True, instrument="TST-A")
    assert len(released) == 1
    d = env.gate.decide(sig(env, RequestType.ENTRY, "TST-B", "10"))
    assert d.result is GateResult.REJECTED and d.failed_check is Check.G2


# --- R-8 정정 소유자 -------------------------------------------------------------------------

def test_r8_modify_owner_must_match_target_order(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(env, RequestType.ENTRY, "TST-A", "50", order_type=OrderType.LIMIT, price="9900"))
    key = env.repo.intent(d.approved.intent_id).broker_order_key
    with env.repo.tx():  # 정지만 기록 (발동 시 미체결 취소 부수효과 없이 G2 판정만 본다)
        env.repo.insert_halt("K9", ACCOUNT, "user", "중지", env.clock.now(), owner="t001-fixed:1", variant="USER")
    req = GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY, account=ACCOUNT,
                      instrument="TST-A", side=Side.BUY, quantity=Decimal("50"), order_type=OrderType.LIMIT,
                      price=Decimal("9950"), idem_key="m-1", owner=None, target_order_key=key)
    d2 = env.gate.decide(req)
    assert d2.result is GateResult.REJECTED and "소유자" in (d2.reason or ""), d2.reason


# --- R-9 정정 후 예약 -------------------------------------------------------------------------

def test_r9_reservation_after_modify_and_partial_fill(env: Env) -> None:
    env.facts.market.upper["TST-A"] = Decimal("20000")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(env, RequestType.ENTRY, "TST-A", "10", order_type=OrderType.LIMIT, price="10000"))
    key = env.repo.intent(d.approved.intent_id).broker_order_key
    env.broker.fill(env.repo.broker_order(key).order_no, Decimal("4"))
    env.executor.process_events()
    m = env.gate.decide(GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY,
                                    account=ACCOUNT, instrument="TST-A", side=Side.BUY, quantity=Decimal("6"),
                                    order_type=OrderType.LIMIT, price=Decimal("15000"), idem_key="m",
                                    owner="t001-fixed:1", target_order_key=key))
    env.executor.submit(m.approved)
    env.executor.process_events()
    new_no = env.repo.broker_order(env.repo.intent(m.approved.intent_id).broker_order_key).order_no
    env.broker.fill(new_no, Decimal("3"))
    env.executor.process_events()
    [r] = env.repo.active_reservations(ACCOUNT)
    assert r.remaining >= Decimal("45000")   # 미체결 3주 × 15,000
    assert r.cash_remaining >= Decimal("45000")


# --- R-10 정지 정책 취소 재시도 ---------------------------------------------------------------

def test_r10_halt_cancel_retried_after_blocking_halt_released(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    d = run(env, sig(env, RequestType.ENTRY, "TST-A", "10"))
    key = env.repo.intent(d.approved.intent_id).broker_order_key
    env.halts.on_start()                                          # K8: 모든 요청 거부
    env.halts.activate(HaltType.K1, "local", "킬")                 # 매수 취소가 K8에 막힘
    assert env.repo.broker_order(key).state == "ACCEPTED"
    env.halts.report_condition(HaltType.K8, None, "recovery_ok", True)
    assert env.halts.resume(HaltType.K8, None, Channel.LOCAL, "u").ok
    env.executor.process_events()
    assert env.repo.broker_order(key).state == "CANCELLED"         # K8 해제 후 K1 정책 취소를 다시 시도


# --- R-11 KILL 파일 삭제 ------------------------------------------------------------------------

def test_r11_clear_kill_file_refused_without_released_k1(tmp_path: Path) -> None:
    db, kill = tmp_path / "t.db", tmp_path / "KILL"
    Repository(db, Mode.BACKTEST).close()
    kill.write_text("KILL")
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "resume.py"), "K1", "--mode", "BACKTEST",
                           "--db", str(db), "--kill-file", str(kill), "--clear-kill-file"],
                          capture_output=True, text=True, timeout=60)
    assert kill.exists(), proc.stdout


# --- A-* 구조 보강 ------------------------------------------------------------------------------

def test_a_submit_inside_open_transaction_refused(env: Env) -> None:
    d = env.gate.decide(sig(env, RequestType.ENTRY))
    with pytest.raises(RuntimeError):
        with env.repo.tx():
            env.executor.submit(d.approved)
    assert env.broker.sent == []


def test_a_resolver_processes_realtime_events_while_waiting(env: Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = run(env, sig(env, RequestType.ENTRY))
    env.broker.inject(OrderEvent(kind="FILL", order_no="9999999", fill_id="X", quantity=Decimal("1"),
                                 price=Decimal("10000")))
    # 통보 반영을 호출하지 않아도 조회 대기 중 반영되어 "짝 없는 통보"로 판단된다 → 미접수 확정 금지
    assert resolver(env).resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS


def test_a_schema_version_mismatch_refused(tmp_path: Path) -> None:
    from trading_system.storage.repository import SchemaMismatch
    p = tmp_path / "s.db"
    r = Repository(p, Mode.BACKTEST)
    r.connection.execute("UPDATE meta SET value='999' WHERE key='schema_version'")
    r.close()
    with pytest.raises(SchemaMismatch):
        Repository(p, Mode.BACKTEST)


def test_a_order_path_rule_covers_modify() -> None:
    from tests.architecture import test_order_path
    assert "modify_order" in test_order_path.ORDER_CALLS


def test_signal_helper_virtual_only() -> None:
    assert make_signal(RequestType.ENTRY).instrument.startswith("TST-")
