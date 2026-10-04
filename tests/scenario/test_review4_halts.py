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


def test_fe_resume_k1_not_rejected_by_unparsable_kill(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    w = World(tmp_path)
    try:
        w.engine.start()
        w.resume_k8()
        w.command("KILL", {"account": None, "reason": "킬"})
        w.engine.tick()
        w.command("RESUME", {"account": None, "halt_type": "K1"})
        w.command("KILL", {"account": "다른-계좌", "reason": "다른 계좌 킬"})   # 이 계좌와 무관
        w.engine.tick()
        rows = w.repo.select("SELECT command, status FROM ops_command ORDER BY created_seq")
        resumes = [r["status"] for r in rows if r["command"] == "RESUME"]
        assert resumes[-1] == "DONE", rows                          # K1 재개는 다른 계좌 KILL에 막히지 않음
    finally:
        w.close()


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
