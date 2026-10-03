"""통합 재검토 후속 (2026-10-03 재검토 F1~F9) — feat/T-016-fix 통합 결함."""

from __future__ import annotations

from tests.scenario.conftest import Env
from tests.scenario.test_rv_halts import _cancels
from tests.scenario.test_t005_halts import _limit_buy
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType


# --- F1: 감시 스레드가 K1 행을 먼저 기록해도 매수 미체결 취소는 판정·실행 스레드에서 한 번 나간다 ----------

def test_f1_kill_recorded_first_by_supervisor_still_cancels_open_buys_once(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.broker.behavior = FakeBehavior.REJECT
    env.halts.record_only(HaltType.K1, "LOCAL:u", "킬")      # 감시 스레드 경로: 행만, 주문 없음
    assert "K1" in env.repo.active_halts(None) or env.repo.halt_rows(None)
    assert _cancels(env) == 0
    env.halts.activate(HaltType.K1, "LOCAL:u", "킬")         # 판정·실행 스레드의 KILL 명령 처리
    assert _cancels(env) == 1
    env.halts.activate(HaltType.K1, "LOCAL:u", "킬 다시")     # 이후 재발동은 재전송 없음 (RV-07)
    assert _cancels(env) == 1
    rows = [r for r in env.repo.halt_rows(None) if r["halt_type"] == "K1"]
    assert len(rows) == 1


# --- F2: 입출력 스레드 종료로 걸린 K5는 대조 성공으로 풀리지 않는다(재시작 후에만) -------------------------

def test_f2_thread_death_k5_not_released_by_reconcile_until_restart(env: Env) -> None:
    from datetime import timedelta

    from tests.scenario.conftest import ACCOUNT
    from trading_system.halt.manager import K5_LINK_REST, K5_LINK_THREAD
    env.halts.activate(HaltType.K5, "thread", "시세 조회 스레드 종료", ACCOUNT)
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REST, True, "조회 성공")
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, "reconciled", True, "대조 일치")
    assert "K5" in env.repo.active_halts(ACCOUNT)                 # 대조로는 죽은 스레드가 살아나지 않는다
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, K5_LINK_THREAD, True, "프로세스 시작")
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, "reconciled", True, "시작 대조 일치")
    assert "K5" not in env.repo.active_halts(ACCOUNT)


# --- F3: 일일 손익 현금은 기준 시각 현금 + 그 뒤 내부 체결 현금흐름 — entr 반영 여부와 무관, 매수·매도 대칭 ---------

def _sell_fill(env: Env, qty: str = "100") -> None:
    from tests.scenario.conftest import make_signal
    from trading_system.domain.enums import RequestType
    from trading_system.domain.models import GateRequest
    env.broker.behavior = FakeBehavior.FILL
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.EXIT, instrument="TST-A", quantity=qty,
                                                            evaluation="f3-exit")))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()


def test_f3_buy_with_entr_already_reflecting_is_not_a_loss(env: Env) -> None:
    from decimal import Decimal

    from tests.scenario.conftest import ACCOUNT
    from tests.scenario.test_rv_halts import _buy_fill, _rv23
    tracker, _ = _rv23(env)
    tracker.start_day(ACCOUNT, Decimal("10000000"))
    _buy_fill(env)
    tracker.update_cash(ACCOUNT, Decimal("8999850"))           # entr가 당일 매수를 이미 반영한 경우
    assert tracker.daily_risk(ACCOUNT).unrealized_change == Decimal("-150")   # 이중 차감 없음 (비용만)


def test_f3_round_trip_sell_is_not_a_false_loss(env: Env) -> None:
    from decimal import Decimal

    from tests.scenario.conftest import ACCOUNT
    from tests.scenario.test_rv_halts import _buy_fill, _rv23
    tracker, _ = _rv23(env)
    tracker.start_day(ACCOUNT, Decimal("10000000"))
    _buy_fill(env)
    _sell_fill(env)
    tracker.update_cash(ACCOUNT, Decimal("10000000"))          # entr가 T+2로 당일 체결을 모두 반영하지 않은 경우
    d = tracker.daily_risk(ACCOUNT)
    # 매수 비용 150 + 매도 수수료 150·세금 2,000 = 2,300만 손실 (매도 대금 미가산이면 -1,000,150으로 K2 오발동)
    assert d.unrealized_change == Decimal("-2300")


# --- F5: 일일 기준 금액을 못 정하면(시세 미수신 등) 다음 tick에 다시 시도 (시도 뒤 10분 안) ------------------

def test_f5_daily_base_retried_after_initial_failure(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    w = World(tmp_path)
    try:
        tracker = w.engine.account_state.tracker
        real = tracker.start_day
        calls = {"n": 0}

        def flaky(account, cash):  # type: ignore[no-untyped-def]
            calls["n"] += 1
            return None if calls["n"] == 1 else real(account, cash)   # 첫 시도: 보유 시세 없음 흉내
        tracker.start_day = flaky
        w.engine.start()
        w.engine.tick()
        assert calls["n"] >= 1
        w.engine.tick()
        assert calls["n"] >= 2                                         # 다시 시도
        assert w.engine.account_state.daily_risk(APP_ACCOUNT) is not None
    finally:
        w.close()


# --- 재검토2 #1: 자정이 지나도 일일 손익 현금흐름은 기준 구간의 거래일로 계산 (거짓 K2 방지) ---------------

def test_f3b_pnl_stable_across_midnight(env: Env) -> None:
    from datetime import timedelta
    from decimal import Decimal

    from tests.scenario.conftest import ACCOUNT
    from tests.scenario.test_rv_halts import _buy_fill, _rv23
    tracker, _ = _rv23(env)
    tracker.start_day(ACCOUNT, Decimal("10000000"))
    _buy_fill(env)
    _sell_fill(env)
    tracker.update_cash(ACCOUNT, Decimal("10000000"))
    before = tracker.daily_risk(ACCOUNT).unrealized_change
    env.clock.advance(timedelta(hours=16))                     # 09:05 → 다음날 01:05 KST: 자정 넘김, 다음 기준 시각 전
    assert tracker.daily_risk(ACCOUNT).unrealized_change == before == Decimal("-2300")


# --- 재검토2 #2: 같은 거래일 재시작은 저장된 일일 기준을 다시 쓴다 (재시작으로 L4 초기화 방지) -------------

def test_f3c_restart_same_day_keeps_daily_base(env: Env) -> None:
    from decimal import Decimal

    from tests.scenario.conftest import ACCOUNT
    from tests.scenario.test_rv_halts import _buy_fill, _rv23
    tracker, _ = _rv23(env)
    tracker.start_day(ACCOUNT, Decimal("10000000"))
    _buy_fill(env)
    _sell_fill(env)
    from datetime import timedelta
    env.clock.advance(timedelta(minutes=30))                   # 체결 30분 뒤 재시작
    restarted, _ = _rv23(env)                                  # 재시작 = 새 추적기(메모리 비어 있음)
    restarted.start_day(ACCOUNT, Decimal("9997700"))           # 재시작 시점 예수금(당일 반영됐다고 가정)
    restarted.update_cash(ACCOUNT, Decimal("9997700"))
    assert restarted.daily_risk(ACCOUNT).unrealized_change == Decimal("-2300")   # 재시작 전 손실 유지


def test_f5b_daily_base_keeps_retrying_slowly_after_window(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from datetime import timedelta

    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    from trading_system.app.engine import BASE_RETRY_WINDOW, BASE_SLOW_RETRY
    w = World(tmp_path)
    try:
        tracker = w.engine.account_state.tracker
        real = tracker.start_day
        state = {"ok": False, "n": 0}

        def flaky(account, cash):  # type: ignore[no-untyped-def]
            state["n"] += 1
            return real(account, cash) if state["ok"] else None
        tracker.start_day = flaky
        w.engine.start()
        w.engine.tick()
        w.clock.advance(BASE_RETRY_WINDOW + timedelta(seconds=1))
        w.engine.tick()                                        # 빠른 재시도 기간 끝
        n = state["n"]
        w.engine.tick()
        assert state["n"] == n                                 # 느린 간격 전엔 시도 안 함
        state["ok"] = True
        w.clock.advance(BASE_SLOW_RETRY)
        w.engine.tick()
        assert w.engine.account_state.daily_risk(APP_ACCOUNT) is not None   # 계속 시도해 회복
    finally:
        w.close()


# --- 재검토2 #4: RESUME(K1) 뒤에 들어온 KILL이 RESUME에 해제되지 않는다 (KILL 먼저 처리 정렬의 역전) ---------

def test_f1b_resume_then_kill_keeps_k1(tmp_path) -> None:  # type: ignore[no-untyped-def]
    from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
    w = World(tmp_path)
    try:
        w.engine.start()
        w.resume_k8()
        w.command("KILL", {"account": None, "reason": "첫 킬"})
        w.engine.tick()
        assert "K1" in w.repo.active_halts(APP_ACCOUNT)
        w.command("RESUME", {"account": None, "halt_type": "K1"})
        w.command("KILL", {"account": None, "reason": "다시 킬"})   # 사용자가 곧바로 다시 킬
        w.engine.tick()
        assert "K1" in w.repo.active_halts(APP_ACCOUNT)              # 마지막 의사(KILL)가 남아야 한다
    finally:
        w.close()


# --- 재조회 전 대기 (01 "재조회 1회 (체결 반영 지연 확인)") — 늦게 온 체결 통보를 기다린 뒤 다시 조회 ----------

def test_requery_waits_once_before_requery(env: Env) -> None:
    from decimal import Decimal

    from tests.scenario.conftest import ACCOUNT
    from trading_system.reconciliation.reconciler import Reconciler
    from trading_system.storage.alerts import AuditAlertSink
    env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("1"))   # 불일치 원인
    waited: list[int] = []

    def wait() -> None:
        waited.append(1)
    rec = Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock),
                     drain_events=env.executor.process_events, requery_wait=wait)
    rec.run(ACCOUNT, "PERIODIC")
    assert waited == [1]                       # 재조회 전에 정확히 한 번 기다린다


# --- 재검토2 #6: 일봉 갱신 중 예상 밖 예외도 실패로 세고 간격을 지킨다(1.2초마다 재시도·시세 갱신 막힘 방지) ---

def test_daily_refresh_unexpected_exception_counts_as_failure_and_quotes_continue() -> None:
    from tests.scenario.test_rv_loop import DAY, Alerts, FakeFeed, kst
    from trading_system.domain.clock import FixedClock
    from trading_system.marketdata.kiwoom_feed import DAILY_RETRY, MarketPoller
    clock = FixedClock(kst(DAY, 10, 0))
    f = FakeFeed(clock)
    quotes: list[str] = []
    f.refresh_quote = lambda code: quotes.append(code)  # type: ignore[method-assign]

    def boom(code: str):  # type: ignore[no-untyped-def]
        f.calls.append(clock.now())
        raise KeyError("저장 중 예상 밖 오류")
    f.refresh_daily = boom  # type: ignore[method-assign]
    p = MarketPoller(f, alerts=Alerts())  # type: ignore[arg-type]
    p.poll_once()
    p.poll_once()
    assert len(f.calls) == 1                     # 간격 전 재시도 없음
    assert quotes                                # 같은 주기의 시세 갱신은 계속
    clock.advance(DAILY_RETRY)
    p.poll_once()
    assert len(f.calls) == 2
