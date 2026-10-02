"""T-005 정지 관리·로컬 운영 명령 (05-plan T-005, 04 §3.2 V-20~V-31, 01 §2.6·§2.9·§2.11, 03 §6.5·§6.6).

각 K에 대해: 요청 유형 6종의 허용/거부(표 시험), 미체결 처리, 보유 유지, 알림, 재개 조건 미충족 시 재개 거부, 충족 시 재개.
모든 테스트 종료 시 conftest가 불변 조건을 검사한다.
"""

from __future__ import annotations

import json
import subprocess
import sys
import threading
import time
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, Env, make_signal
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import (
    Check, GateResult, HaltType, IntentState, OrderType, RequestSource, RequestType, Side,
)
from trading_system.domain.halts import HaltState
from trading_system.domain.market import DailyRisk
from trading_system.domain.models import GateRequest
from trading_system.execution.executor import ExecutionBlocked
from trading_system.execution.resolver import ResolveOutcome, UnknownResolver
from trading_system.halt.manager import Channel
from trading_system.ops.commands import OpsProcessor
from trading_system.storage.repository import StorageFailure

ROOT = Path(__file__).resolve().parents[2]
OWNER = "t001-fixed:1"


# --- 준비 ---------------------------------------------------------------------------------

def _sig(kind: RequestType, inst: str, qty: str, ev: str) -> GateRequest:
    return GateRequest.from_signal(make_signal(kind, instrument=inst, quantity=qty, evaluation=ev))


def _limit_buy(env: Env, inst: str, ev: str) -> str:
    s = make_signal(RequestType.ENTRY, instrument=inst, quantity="50", evaluation=ev)
    req = GateRequest(source=RequestSource.SIGNAL, request_type=RequestType.ENTRY, account=ACCOUNT, instrument=inst,
                      side=Side.BUY, quantity=Decimal("50"), order_type=OrderType.LIMIT, price=Decimal("10000"),
                      idem_key=s.signal_id, owner=s.owner, valid_until=s.valid_until, signal_id=s.signal_id)
    d = env.gate.decide(req)
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    return env.repo.intent(d.approved.intent_id).broker_order_key


def _hold(env: Env, inst: str, qty: str = "50") -> None:
    behavior = env.broker.behavior
    env.broker.behavior = FakeBehavior.FILL
    d = env.gate.decide(_sig(RequestType.ENTRY, inst, qty, f"h-{inst}"))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    env.broker.behavior = behavior


def prepare(env: Env) -> dict[str, str]:
    """보유 TST-A·TST-D, 미체결 매수 TST-B(취소 대상)·TST-E(정정 대상)."""
    _hold(env, "TST-A")
    _hold(env, "TST-D")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    return {"B": _limit_buy(env, "TST-B", "ob"), "E": _limit_buy(env, "TST-E", "oe")}


def six_requests(orders: dict[str, str]) -> dict[str, GateRequest]:
    return {
        "ENTRY": _sig(RequestType.ENTRY, "TST-C", "10", "m-entry"),
        "INCREASE": _sig(RequestType.INCREASE, "TST-A", "10", "m-inc"),
        "REDUCE_SIGNAL": _sig(RequestType.REDUCE, "TST-A", "10", "m-red"),
        "EXIT_OPERATOR": GateRequest(source=RequestSource.OPERATOR, request_type=RequestType.EXIT, account=ACCOUNT,
                                     instrument="TST-D", side=Side.SELL, quantity=Decimal("50"),
                                     order_type=OrderType.MARKET, price=Decimal("10000"), idem_key="m-exit"),
        "CANCEL_OPERATOR": GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, "TST-B", Side.BUY, orders["B"],
                                              idem_key="m-cancel", owner=None),
        "MODIFY_BUY": GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY, account=ACCOUNT,
                                  instrument="TST-E", side=Side.BUY, quantity=Decimal("50"), order_type=OrderType.LIMIT,
                                  price=Decimal("9995"), idem_key="m-mod", owner=OWNER, target_order_key=orders["E"]),
    }


ORDER = ("ENTRY", "INCREASE", "REDUCE_SIGNAL", "EXIT_OPERATOR", "CANCEL_OPERATOR", "MODIFY_BUY")
T, F = True, False

# 01 §2.6 표 → 요청 6종 허용 여부 (K3 범위 종목은 TST-Z: 취소 대상 TST-B와 다름)
MATRIX = [
    ("K1", {}, (F, F, F, T, T, F)),
    ("K2", {}, (F, F, T, T, T, F)),
    ("K3", {"instrument": "TST-Z"}, (F, F, F, F, T, F)),
    ("K3", {"instrument": "TST-B"}, (F, F, F, F, F, F)),  # 결과 불명 종목의 취소는 거부
    ("K4", {}, (F, F, F, F, T, F)),
    ("K5", {}, (F, F, F, F, F, F)),
    ("K6", {}, (F, F, F, T, T, F)),
    ("K7", {}, (F, F, F, F, F, F)),
    ("K8", {}, (F, F, F, F, F, F)),
    ("K9", {"owner": OWNER, "variant": "USER"}, (F, F, F, T, T, F)),
    ("K9", {"owner": OWNER, "variant": "REPLACE"}, (F, F, T, T, T, F)),
    ("K9", {"owner": "other:1", "variant": "USER"}, (T, T, T, T, T, T)),
    ("K10", {"instrument": "TST-A"}, (T, F, T, T, T, T)),
    ("K10", {}, (F, F, T, T, T, F)),
    ("K11", {}, (F, F, T, T, T, F)),
]


@pytest.mark.parametrize(("k", "scope", "expected"), MATRIX,
                         ids=[f"{k}-{'-'.join(map(str, s.values())) or 'acct'}" for k, s, _ in MATRIX])
def test_v20_v31_six_request_matrix(env: Env, k: str, scope: dict, expected: tuple[bool, ...]) -> None:
    orders = prepare(env)
    with env.repo.tx():
        env.repo.insert_halt(k, ACCOUNT, "test", "표 시험", env.clock.now(), **scope)
    reqs = six_requests(orders)
    got = {}
    for name in ORDER:
        d = env.gate.decide(reqs[name])
        got[name] = d.result is GateResult.APPROVED
        if not got[name] and d.failed_check is not Check.G2:
            pytest.fail(f"{k} {name}: G2가 아닌 {d.failed_check}로 거부 — {d.reason}")
    assert tuple(got[n] for n in ORDER) == expected, got


def test_v31_multiple_halts_strictest_wins_and_resume_separately(env: Env) -> None:
    orders = prepare(env)
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    with env.repo.tx():
        env.repo.insert_halt("K2", ACCOUNT, "test", "손실", env.clock.now())
    reqs = six_requests(orders)
    assert env.gate.decide(reqs["REDUCE_SIGNAL"]).failed_check is Check.G2  # K2는 허용, K4가 거부
    assert env.gate.decide(reqs["CANCEL_OPERATOR"]).result is GateResult.APPROVED
    env.clock.advance(timedelta(days=1))
    assert env.halts.resume(HaltType.K2, ACCOUNT, Channel.LOCAL, "u").ok
    assert env.repo.active_halts(ACCOUNT) == ["K4"]  # K4는 자기 조건으로만 풀림


# --- V-20 K1 ------------------------------------------------------------------------------

def _ops(env: Env) -> OpsProcessor:
    return OpsProcessor(env.repo, env.gate, env.executor, env.halts, env.clock)


def _command(env: Env, cid: str, name: str, args: dict, channel: str = Channel.LOCAL) -> bool:
    with env.repo.tx():
        return env.repo.insert_command(cid, channel, "user", name, args, env.clock.now())


def _alerts(env: Env, level: str) -> list[str]:
    return [json.loads(e["summary"])["message"] for e in env.repo.audit_events()
            if e["kind"] == "ALERT" and json.loads(e["summary"])["level"] == level]


def test_v20_k1_via_command_cancels_buys_keeps_sells_and_positions(env: Env) -> None:
    orders = prepare(env)
    d = env.gate.decide(_sig(RequestType.REDUCE, "TST-A", "20", "sell"))  # 미체결 매도
    env.executor.submit(d.approved)
    sell_key = env.repo.intent(d.approved.intent_id).broker_order_key
    _command(env, "c-kill", "KILL", {"account": None, "reason": "test"})
    [out] = _ops(env).process_pending()
    assert out.status == "DONE"
    env.executor.process_events()
    assert env.repo.broker_order(orders["B"]).state == "CANCELLED"
    assert env.repo.broker_order(orders["E"]).state == "CANCELLED"
    assert env.repo.broker_order(sell_key).state == "ACCEPTED"  # 매도 미체결 유지
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("50")  # 자동 청산 없음
    assert not any(r.kind == "NEW" and r.side == "SELL" and r.instrument == "TST-D" for r in env.broker.sent)
    assert any("K1" in m for m in _alerts(env, "EMERGENCY"))
    # 원격 재개 가능 (01 §2.9)
    _command(env, "c-resume", "RESUME", {"account": None, "halt_type": "K1"}, Channel.REMOTE)
    [r] = _ops(env).process_pending()
    assert r.status == "DONE" and env.repo.active_halts(ACCOUNT) == []


def test_v20_kill_activation_is_idempotent(env: Env) -> None:
    a = env.halts.activate(HaltType.K1, "local", "x")
    b = env.halts.activate(HaltType.K1, "remote", "y")
    assert a == b and env.repo.active_halts(ACCOUNT) == ["K1"]


# --- V-21 K2 ------------------------------------------------------------------------------

def test_v21_k2_auto_on_loss_not_auto_released_user_resume_after_reference_time(env: Env) -> None:
    orders = prepare(env)
    lim = env.facts.limits.by_account[ACCOUNT]
    assert env.halts.check_daily_loss(ACCOUNT, DailyRisk(Decimal("10000000"), Decimal("-199999"), Decimal("0"),
                                                         Decimal("0")), lim.l4_daily_loss_ratio) is None
    hid = env.halts.check_daily_loss(ACCOUNT, DailyRisk(Decimal("10000000"), Decimal("-200000"), Decimal("0"),
                                                        Decimal("0")), lim.l4_daily_loss_ratio)
    assert hid is not None
    env.executor.process_events()
    assert env.repo.broker_order(orders["B"]).state == "CANCELLED"  # 매수 미체결 취소
    reqs = six_requests(orders)
    assert env.gate.decide(reqs["REDUCE_SIGNAL"]).result is GateResult.APPROVED  # 전략의 감액 허용
    r = env.halts.resume(HaltType.K2, ACCOUNT, Channel.REMOTE, "u")
    assert not r.ok and "기준 시각" in r.reason
    env.clock.advance(timedelta(hours=23, minutes=1))  # 다음 날 08:06 KST
    assert env.repo.active_halts(ACCOUNT) == ["K2"]  # 자동 해제 없음
    assert env.halts.resume(HaltType.K2, ACCOUNT, Channel.REMOTE, "u").ok  # 원격 재개 허용


# --- V-22 K3 ------------------------------------------------------------------------------

def test_v22_k3_from_resolver_local_resume_only_after_human_confirm_and_reconcile(env: Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    d = env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "u"))
    env.executor.submit(d.approved)
    env.broker.query_failures = 1
    r = UnknownResolver(env.repo, env.broker, env.executor, env.clock, lambda s: None)
    assert r.resolve(d.approved.intent_id) is ResolveOutcome.AMBIGUOUS
    assert "K3" in env.repo.active_halts(ACCOUNT)
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.REMOTE, "u").ok
    res = env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u")
    assert not res.ok and "결과 불명" in res.reason
    # 사람 확정은 로컬 명령만
    _command(env, "c-r", "CONFIRM_UNKNOWN", {"intent_id": d.approved.intent_id, "accepted": False}, Channel.REMOTE)
    _command(env, "c-l", "CONFIRM_UNKNOWN", {"intent_id": d.approved.intent_id, "accepted": False})
    remote, local = _ops(env).process_pending()
    assert remote.status == "REJECTED" and local.status == "DONE"
    assert env.repo.intent(d.approved.intent_id).state is IntentState.NOT_ACCEPTED
    assert not env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok  # 대조 일치 전
    env.halts.report_condition(HaltType.K3, ACCOUNT, "reconciled", True, "test")
    assert env.halts.resume(HaltType.K3, ACCOUNT, Channel.LOCAL, "u").ok


def test_v22_k3_human_confirm_accepted_adopts_order(env: Env) -> None:
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    d = env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "u"))
    env.executor.submit(d.approved)
    order_no = env.broker.query_orders(ACCOUNT)[-1].order_no  # 사람이 증권사 화면에서 확인한 주문번호
    env.halts.confirm_unknown(d.approved.intent_id, True, "user", order_no)
    assert env.repo.intent(d.approved.intent_id).state is IntentState.ACCEPTED


# --- V-23·V-24·V-25·V-30 조건과 해제 ---------------------------------------------------------

def test_v23_k4_resume_needs_reconcile_and_condition_can_regress(env: Env) -> None:
    hid = env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    assert not env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok
    env.halts.report_condition(HaltType.K4, ACCOUNT, "reconciled", True)
    assert env.repo.halt(hid)["state"] == HaltState.AWAITING_RESUME.value
    env.halts.report_condition(HaltType.K4, ACCOUNT, "reconciled", False)  # 다시 불일치
    assert env.repo.halt(hid)["state"] == HaltState.ACTIVE.value
    assert not env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok
    env.halts.report_condition(HaltType.K4, ACCOUNT, "reconciled", True)
    assert env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "u").ok


def test_v24_k5_auto_release_needs_connection_and_reconcile(env: Env) -> None:
    hid = env.halts.activate(HaltType.K5, "monitor", "API 단절", ACCOUNT)
    env.halts.report_condition(HaltType.K5, ACCOUNT, "broker_connected", True)
    assert env.repo.halt(hid)["state"] == HaltState.ACTIVE.value
    assert env.halts.report_condition(HaltType.K5, ACCOUNT, "reconciled", True) == [hid]
    assert env.repo.active_halts(ACCOUNT) == []


@pytest.mark.parametrize(("k", "cond"), [(HaltType.K6, "clock_ok"), (HaltType.K10, "data_ok"),
                                         (HaltType.K11, "notify_ok")])
def test_v25_v29_v30_auto_release_types(env: Env, k: HaltType, cond: str) -> None:
    env.halts.activate(k, "monitor", "test", ACCOUNT)
    blocked = env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "b"))
    assert blocked.failed_check is Check.G2
    assert env.halts.report_condition(k, ACCOUNT, cond, False) == []
    assert env.halts.report_condition(k, ACCOUNT, cond, True)
    assert env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "b2")).result is GateResult.APPROVED


# --- V-26 K7 ------------------------------------------------------------------------------

def test_v26_storage_failure_after_send_records_k7_and_stays_blocked(env: Env) -> None:
    d = env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "s"))
    original = env.broker.place_order

    def place_then_break(req):
        result = original(req)
        env.repo.fail_next_transaction = True
        return result

    env.broker.place_order = place_then_break  # type: ignore[method-assign]
    with pytest.raises(StorageFailure):
        env.executor.submit(d.approved)
    env.broker.place_order = original  # type: ignore[method-assign]
    assert "K7" in env.repo.active_halts(ACCOUNT)
    assert any("K7" in m for m in _alerts(env, "EMERGENCY"))
    assert not env.halts.resume(HaltType.K7, ACCOUNT, Channel.REMOTE, "u").ok
    assert not env.halts.resume(HaltType.K7, ACCOUNT, Channel.LOCAL, "u").ok
    env.halts.report_condition(HaltType.K7, ACCOUNT, "storage_ok", True)
    assert env.halts.resume(HaltType.K7, ACCOUNT, Channel.LOCAL, "u").ok
    d2 = env.gate.decide(_sig(RequestType.ENTRY, "TST-B", "10", "s2"))
    with pytest.raises(ExecutionBlocked):  # 실행기 차단은 재시작·대조 전까지 유지 (CP-016 B-4)
        env.executor.submit(d2.approved)


# --- V-27 K8 ------------------------------------------------------------------------------

def test_v27_k8_on_start_no_orders_until_user_resume(env: Env) -> None:
    hid = env.halts.on_start()
    assert env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "a")).failed_check is Check.G2
    env.halts.report_condition(HaltType.K8, None, "recovery_ok", True, "대조 일치")
    assert env.repo.halt(hid)["state"] == HaltState.AWAITING_RESUME.value
    assert env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "b")).failed_check is Check.G2  # 재개 전 0건
    assert env.broker.sent == []
    assert not env.halts.resume(HaltType.K8, None, Channel.REMOTE, "u").ok
    assert env.halts.resume(HaltType.K8, None, Channel.LOCAL, "u").ok
    assert env.gate.decide(_sig(RequestType.ENTRY, "TST-A", "10", "c")).result is GateResult.APPROVED


# --- V-28 K9 ------------------------------------------------------------------------------

def test_v28_k9_cancels_only_that_strategys_open_buys(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    mine = _limit_buy(env, "TST-A", "x1")
    s = make_signal(RequestType.ENTRY, instrument="TST-B", strategy="other", evaluation="x2")
    other = env.gate.decide(GateRequest.from_signal(s))
    env.executor.submit(other.approved)
    env.halts.activate(HaltType.K9, "user", "교체", ACCOUNT, owner=OWNER, variant="REPLACE")
    env.executor.process_events()
    assert env.repo.broker_order(mine).state == "CANCELLED"
    assert env.repo.intent(other.approved.intent_id).state is IntentState.ACCEPTED


# --- V-32 원격 재개 범위 (채널 연동은 T-013) ----------------------------------------------------

@pytest.mark.parametrize("k", [HaltType.K3, HaltType.K4, HaltType.K7, HaltType.K8])
def test_v32_remote_resume_refused(env: Env, k: HaltType) -> None:
    env.halts.activate(k, "test", "x", ACCOUNT)
    r = env.halts.resume(k, ACCOUNT, Channel.REMOTE, "u")
    assert not r.ok and "로컬" in r.reason
    assert k.value in env.repo.active_halts(ACCOUNT)


# --- 운영 명령 -------------------------------------------------------------------------------

def test_ops_duplicate_command_id_processed_once(env: Env) -> None:
    assert _command(env, "same", "KILL", {})
    assert not _command(env, "same", "KILL", {})
    assert len(_ops(env).process_pending()) == 1
    assert _ops(env).process_pending() == []


def test_ops_entry_command_refused_and_reduce_goes_through_gate(env: Env) -> None:
    _hold(env, "TST-A")
    _command(env, "c1", "ENTRY", {"account": ACCOUNT, "instrument": "TST-B", "quantity": "10"})
    _command(env, "c2", "REDUCE", {"account": ACCOUNT, "instrument": "TST-A", "quantity": "999"})  # 보유 초과
    _command(env, "c3", "EXIT", {"account": ACCOUNT, "instrument": "TST-A", "quantity": "50"})
    o1, o2, o3 = _ops(env).process_pending()
    assert o1.status == "REJECTED"
    assert o2.status == "REJECTED" and "G9" in o2.result
    assert any("운영 요청 거부" in m for m in _alerts(env, "EMERGENCY"))  # 위험 축소 거부는 알림
    assert o3.status == "DONE"
    env.executor.process_events()
    assert env.repo.position(ACCOUNT, "TST-A").quantity == 0


# --- C-4 다른 프로세스(scripts/kill)가 같은 DB에 쓰기 ----------------------------------------------

def test_c4_kill_script_from_other_process_while_main_holds_write_lock(env: Env, tmp_path: Path) -> None:
    db = tmp_path / "trading.db"
    kill = tmp_path / "KILL"
    hold_done = threading.Event()

    def hold_lock() -> None:
        with env.repo.tx():  # 본 프로세스가 1초 동안 쓰기 잠금을 잡고 있음
            env.repo.audit(env.clock.now(), "TEST_LOCK", "test", None, {})
            time.sleep(1.0)
        hold_done.set()

    t = threading.Thread(target=hold_lock)
    t.start()
    time.sleep(0.1)
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "kill.py"), "--mode", "BACKTEST",
                           "--db", str(db), "--kill-file", str(kill), "--reason", "c4"],
                          capture_output=True, text=True, timeout=60)
    t.join()
    assert proc.returncode == 0, proc.stdout + proc.stderr
    assert kill.exists() and hold_done.is_set()
    [out] = _ops(env).process_pending()
    assert out.status == "DONE" and env.repo.active_halts(ACCOUNT) == ["K1"]


def test_c4_script_refuses_db_of_other_mode(env: Env, tmp_path: Path) -> None:
    proc = subprocess.run([sys.executable, str(ROOT / "scripts" / "kill.py"), "--mode", "PAPER",
                           "--db", str(tmp_path / "trading.db"), "--kill-file", str(tmp_path / "K")],
                          capture_output=True, text=True, timeout=60)
    assert proc.returncode != 0
    assert (tmp_path / "K").exists()  # KILL 파일은 저장소와 무관하게 생성됨
    assert env.repo.pending_commands() == []
