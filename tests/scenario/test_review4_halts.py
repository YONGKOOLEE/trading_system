"""4차 검토 후속 (2026-10-04, A-045): 자동 해제 재시도 범위(F-A)·송신 스레드(F-B)·RESUME K1 거부 판정(F-E)."""

from __future__ import annotations

import threading
from datetime import timedelta

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_rv_halts import _cancels
from tests.scenario.test_t005_halts import _limit_buy
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType


def test_fa_broker_rejected_cancel_not_resent_on_unrelated_auto_release(env: Env) -> None:
    """브로커가 거부한 취소는 관계없는 정지의 자동 해제로 다시 보내지 않는다 (CLAUDE.md §8-7)."""
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.broker.behavior = FakeBehavior.REJECT
    env.halts.activate(HaltType.K1, "LOCAL:u", "kill", None)
    n0 = _cancels(env)
    for _ in range(3):
        env.clock.advance(timedelta(seconds=1))
        env.halts.activate(HaltType.K6, "monitor", "clock", None)
        env.halts.report_condition(HaltType.K6, None, "clock_ok", True, "ok")
    assert _cancels(env) == n0


def test_fb_retry_not_sent_from_non_owner_thread(env: Env) -> None:
    """송신 스레드가 정해져 있으면 다른 스레드의 자동 해제는 취소를 보내지 않는다 (D-038)."""
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.halts.activate(HaltType.K5, "recovery", "조회 실패", ACCOUNT)
    env.halts.activate(HaltType.K1, "LOCAL:u", "kill", None)        # K5에 막힘
    env.halts.send_thread = threading.get_ident()                     # 판정·실행 스레드 = 이 스레드
    sent: list[str] = []
    real = env.executor.submit

    def spy(a):  # type: ignore[no-untyped-def]
        sent.append(threading.current_thread().name)
        return real(a)
    env.executor.submit = spy  # type: ignore[method-assign]
    t = threading.Thread(target=lambda: (
        env.halts.report_condition(HaltType.K5, ACCOUNT, "broker_connected", True, "x"),
        env.clock.advance(timedelta(seconds=1)),
        env.halts.report_condition(HaltType.K5, ACCOUNT, "reconciled", True, "y")), name="market-poller")
    t.start()
    t.join()
    assert sent == []


def test_fe_account_resume_k1_not_rejected_by_other_account_kill(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """계좌 RESUME K1은 다른 계좌 KILL에 막히지 않는다(그 KILL의 K1 행은 이 RESUME으로 안 풀림) — 5차 검토로 수정."""
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    w = World(tmp_path)
    try:
        w.engine.start()
        w.resume_k8()
        w.command("KILL", {"account": APP_ACCOUNT, "reason": "킬"})
        w.engine.tick()
        w.command("RESUME", {"account": APP_ACCOUNT, "halt_type": "K1"})
        w.command("KILL", {"account": "다른-계좌", "reason": "다른 계좌 킬"})
        w.engine.tick()
        rows = w.repo.select("SELECT command, status FROM ops_command ORDER BY created_seq")
        resumes = [r["status"] for r in rows if r["command"] == "RESUME"]
        assert resumes[-1] == "DONE", rows
        assert "K1" not in w.repo.active_halts(APP_ACCOUNT)
    finally:
        w.close()


def test_f1b_global_resume_then_account_kill_keeps_k1(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """전체 계좌 RESUME K1(계좌 K1 행까지 푼다) 뒤에 들어온 계좌 KILL은 남아야 한다 (5차 검토 F-1)."""
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    w = World(tmp_path)
    try:
        w.engine.start()
        w.resume_k8()
        w.command("KILL", {"account": None, "reason": "킬"})
        w.engine.tick()
        w.command("RESUME", {"account": None, "halt_type": "K1"})
        w.command("KILL", {"account": APP_ACCOUNT, "reason": "계좌 킬"})
        w.engine.tick()
        assert "K1" in w.repo.active_halts(APP_ACCOUNT)
    finally:
        w.close()


def test_f2b_user_resume_retries_broker_rejected_cancel(env: Env) -> None:
    """사용자 재개는 브로커가 거부했던 매수 취소도 다시 시도한다(사람의 명시적 행동, CP-018 R-10) — 5차 검토 F-2."""
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.broker.behavior = FakeBehavior.REJECT
    env.halts.activate(HaltType.K1, "LOCAL:u", "kill", None)
    n0 = _cancels(env)
    env.halts.activate(HaltType.K6, "monitor", "clock", None)
    env.halts.report_condition(HaltType.K6, None, "clock_ok", True, "ok")      # 자동 해제: 재전송 없음
    assert _cancels(env) == n0
    env.halts.activate(HaltType.K4, "reconciler", "불일치", ACCOUNT)
    env.halts.report_condition(HaltType.K4, ACCOUNT, "reconciled", True, "ok")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    r = env.halts.resume(HaltType.K4, ACCOUNT, "LOCAL", "u")                   # 사용자 재개: 다시 시도
    assert r.ok, r.reason
    assert _cancels(env) == n0 + 1


# --- F-C: 마감 후 대조에서 브로커 미표시(조회 누락과 구별 불가)는 만료 증거가 아니다 -------------------------

def test_fc_close_reconcile_missing_broker_order_not_expired(env: Env) -> None:
    from tests.scenario.test_review3_reconcile import _closing_rec, _partial_open_buy
    no, iid = _partial_open_buy(env)
    real = env.broker.query_orders
    env.broker.query_orders = lambda acct: [o for o in real(acct) if o.order_no != no]  # type: ignore[method-assign]  # 조회에 안 보임
    r = _closing_rec(env).run(ACCOUNT, "CLOSE")
    assert r.result == "MISMATCH"
    assert env.repo.intent(iid).state.value == "PARTIAL"          # 만료 종결하지 않음
    assert "K4" in env.repo.active_halts(ACCOUNT)                  # 사람 확인


def test_fc_close_reconcile_retried_after_query_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from tests.scenario.test_t020_app import World
    w = World(tmp_path)
    try:
        w.engine.start()
        w.resume_k8()
        w.at(15, 31)
        w.broker.disconnected = True
        w.engine.tick()                                             # 마감 후 대조 실패
        triggers = lambda: [r["trigger"] for r in w.repo.select(  # noqa: E731
            "SELECT trigger FROM reconciliation ORDER BY created_seq")]
        assert triggers()[-1] == "CLOSE"
        w.broker.disconnected = False
        w.at(15, 33)
        w.engine.tick()                                             # 대조 주기 뒤 다시 CLOSE
        rows = w.repo.select("SELECT trigger, result FROM reconciliation WHERE trigger='CLOSE' ORDER BY created_seq")
        assert [r["result"] for r in rows][-1] != "QUERY_FAILED" and len(rows) >= 2, rows
        n = len(rows)
        w.at(15, 40)
        w.engine.tick()
        assert len(w.repo.select("SELECT 1 FROM reconciliation WHERE trigger='CLOSE'")) == n   # 성공 뒤엔 다시 안 함
    finally:
        w.close()


# --- 6차 검토 -----------------------------------------------------------------------------------------

def test_r6_f2_presend_failed_cancel_retried_on_auto_release(env: Env) -> None:
    """전송 전 실패(NOT_SENT)한 취소는 브로커에 간 적이 없으므로 자동 해제 때 재시도한다."""
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.broker.behavior = FakeBehavior.PRE_SEND_FAIL
    env.halts.activate(HaltType.K1, "LOCAL:u", "kill", None)
    sent = env.repo.select("SELECT a.state FROM attempt a JOIN intent i ON a.intent_id=i.intent_id "
                           "WHERE i.request_type='CANCEL'")
    assert [r["state"] for r in sent] == ["NOT_SENT"]
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    env.clock.advance(timedelta(seconds=1))
    env.halts.activate(HaltType.K6, "monitor", "clock", None)
    env.halts.report_condition(HaltType.K6, None, "clock_ok", True, "ok")
    sent = env.repo.select("SELECT a.state FROM attempt a JOIN intent i ON a.intent_id=i.intent_id "
                           "WHERE i.request_type='CANCEL' ORDER BY a.created_seq")
    assert [r["state"] for r in sent][0] == "NOT_SENT" and len(sent) == 2   # 자동 해제 때 실제 전송 재시도


def _world_entry(tmp_path, behavior):  # type: ignore[no-untyped-def]
    from tests.scenario.test_t020_app import World
    w = World(tmp_path, behavior)
    w.engine.start()
    w.resume_k8()
    w.at(9, 5)
    w.engine.tick()
    return w


def test_r6_f1_unknown_buy_at_kill_cancelled_once_adopted(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """KILL 때 결과 불명이던 매수가 나중에 접수로 확정되면(해소기 FOUND) 그 주문을 한 번 취소한다."""
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT
    w = _world_entry(tmp_path, FakeBehavior.LOST_ACCEPT)
    try:
        assert [i["state"] for i in w.intents()] == ["UNKNOWN"]
        w.broker.behavior = FakeBehavior.ACCEPT_ONLY
        w.command("KILL", {"account": None, "reason": "킬"})
        w.engine.tick()
        for s in (2, 4, 8, 16, 32, 5):                                # 해소기 회차
            w.clock.advance(timedelta(seconds=s))
            w.engine.tick()
        assert "K1" in w.repo.active_halts(APP_ACCOUNT)
        assert w.repo.open_orders(APP_ACCOUNT, "BUY") == [], w.intents()
        assert sum(1 for i in w.intents() if i["request_type"] == "CANCEL") == 1
    finally:
        w.close()


def test_r6_f3_exec_policy_cancel_retried_after_halt_and_no_row_flood(tmp_path) -> None:  # type: ignore[no-untyped-def]
    """미체결 시간 취소가 정지에 막혀도 정지가 풀리면 다시 판정되고, 막힌 동안 판정 행이 tick마다 쌓이지 않는다."""
    from dataclasses import replace
    from datetime import datetime

    from tests.scenario.test_t020_app import (ACCOUNT as A, CAPS_ALL_SUPPORTED, PARAMS, SETTINGS, CrossStrategy,
                                              World, make_store, quote, scenario_limits)
    from tests.support.sample_strategies import cross_spec
    from trading_system.app.wiring import StrategyInput, build_engine
    from trading_system.domain.enums import Mode
    from trading_system.storage.repository import Repository
    w = World(tmp_path, FakeBehavior.ACCEPT_ONLY)
    try:
        caps = w.engine.strategies[0].strategy.spec.capabilities
        # [CP-032 D-175] 취소만 쓰는 전략은 "CAP-ORD-03b"로 선언("CAP-ORD-03" = 03a 정정 포함 → 등록 거부). 시험 동작 그대로
        spec = cross_spec(version=2, capabilities=tuple(caps) + ("CAP-ORD-03b",), unfilled_policy="미체결 10분 후 취소")

        class S(CrossStrategy):
            pass
        w.repo.close()
        w.repo = Repository(w.db, Mode.BACKTEST)
        w.store = make_store(w.clock)
        quote(w.store, w.clock)
        w.engine = build_engine(mode=Mode.BACKTEST, repo=w.repo, clock=w.clock, broker=w.broker, store=w.store,
                                account=A, account_limits=scenario_limits(), gate_settings=SETTINGS,
                                caps=CAPS_ALL_SUPPORTED, kill_file=tmp_path / "KILL", clock_health=w.health,
                                strategies=[StrategyInput(S(spec), dict(PARAMS), (datetime(2000, 1, 1, 9, 5).time(),))])
        w.engine.strategies[0] = replace(w.engine.strategies[0], unfilled_minutes=10)
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        w.engine.tick()
        # 7차 F-2: 대조로 저절로 풀리지 않는 K8로 막는다(K5는 같은 tick 대조에서 자동 해제돼 막힘을 검증 못 했음)
        w.engine.halts.activate(HaltType.K8, "monitor", "시험: 실행 중 K8", None)
        cancels = lambda: w.repo.select(  # noqa: E731
            "SELECT result, failed_check FROM gate_decision WHERE request_type='CANCEL' ORDER BY seq")
        for s in range(10):                                                     # 10초 동안 tick
            w.at(9, 15, 1 + s)
            w.engine.tick()
        blocked = cancels()
        assert len(blocked) == 1 and blocked[0]["result"] == "REJECTED", blocked   # 1분 안엔 한 번만
        w.at(9, 16, 5)
        w.engine.tick()
        assert len(cancels()) == 2                                              # 1분 뒤 새 키로 재판정
        assert not any(i["request_type"] == "CANCEL" for i in w.intents())
        w.engine.halts.report_condition(HaltType.K8, None, "recovery_ok", True, "시험")
        assert w.engine.halts.resume(HaltType.K8, None, "LOCAL", "u").ok
        w.at(9, 17, 10)
        w.engine.tick()
        assert any(i["request_type"] == "CANCEL" for i in w.intents()), (w.intents(), cancels())
    finally:
        w.close()


def test_r6_f1b_confirm_unknown_accepted_under_kill_cancels_buy(env: Env) -> None:
    """사람이 결과 불명 매수를 접수로 확정했을 때도 K1 중이면 그 주문을 한 번 취소한다."""
    from trading_system.domain.enums import IntentState
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    _limit_buy(env, "TST-B", "ob")
    [unk] = env.repo.intents_in_state(IntentState.UNKNOWN)
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    env.halts.activate(HaltType.K1, "LOCAL:u", "kill", None)
    [no] = env.broker.open_order_nos()
    n0 = _cancels(env)
    env.halts.confirm_unknown(unk.intent_id, True, "u", no)
    assert _cancels(env) == n0 + 1


def test_r6_f4_account_resume_does_not_release_global_k1(env: Env) -> None:
    """계좌 지정 RESUME K1은 전체 계좌 KILL 행을 풀지 않는다(다른 계좌의 KILL까지 사라짐 방지) — 전체 RESUME 필요."""
    env.halts.activate(HaltType.K1, "LOCAL:u", "kill all", None)
    r = env.halts.resume(HaltType.K1, ACCOUNT, "LOCAL", "u")
    assert not r.ok and "전체" in r.reason
    assert [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K1"]
    assert env.halts.resume(HaltType.K1, None, "LOCAL", "u").ok
