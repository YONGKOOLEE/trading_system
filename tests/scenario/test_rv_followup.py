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
