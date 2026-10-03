"""PAPER-0 전 검토 결함 수정 (docs/changes/review-2026-10-03-pre-paper0.md, A-043) — 정지·대조·게이트 쪽.

RV-01 대조 불일치 해소 배선, RV-02 K5 broker_connected, RV-03 K7 storage_ok, RV-07 정지 유지 중 취소 반복,
RV-20 전략 도구 K9 동기화, RV-23 현금 신선도·당일 체결 보수 반영, RV-24 대조 재조회 1회. 모든 값은 가상이다.
"""

from __future__ import annotations

from datetime import timedelta
from decimal import Decimal
from pathlib import Path

from tests.scenario.conftest import ACCOUNT, Env
from tests.scenario.test_t005_halts import _limit_buy, _hold
from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT, World
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType
from trading_system.domain.market import DailyRisk


def _cmd_rows(repo) -> list[dict]:  # type: ignore[no-untyped-def]
    return [dict(r) for r in repo.select("SELECT command, status, result FROM ops_command ORDER BY rowid")]


# --- RV-01 대조 불일치 수동 해소가 실제 배선(build_engine)으로 동작 -------------------------------------

def test_rv01_reconcile_resolve_through_real_wiring(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.engine.start()
        w.broker.add_external_order(APP_ACCOUNT, "TST-B", "BUY", Decimal("1"))
        w.command("RECONCILE", {"account": APP_ACCOUNT})
        w.engine.tick()
        [item] = w.repo.open_recon_items(APP_ACCOUNT)
        assert "K4" in w.repo.active_halts(APP_ACCOUNT)
        w.command("RECONCILE_RESOLVE", {"item_id": item["item_id"], "method": "EXTERNAL"})
        w.engine.tick()
        last = _cmd_rows(w.repo)[-1]
        assert last["command"] == "RECONCILE_RESOLVE" and last["status"] == "DONE", last
        assert w.repo.open_recon_items(APP_ACCOUNT) == []
    finally:
        w.close()


# --- RV-07 정지 유지 중 매 tick 재발동이 취소를 반복 전송하지 않음 -------------------------------------

def _k2(env: Env) -> str | None:
    lim = env.facts.limits.by_account[ACCOUNT]
    return env.halts.check_daily_loss(ACCOUNT, DailyRisk(Decimal("10000000"), Decimal("-300000"), Decimal("0"),
                                                         Decimal("0")), lim.l4_daily_loss_ratio)


def _cancels(env: Env) -> int:
    return sum(1 for r in env.broker.sent if r.target_order_no is not None)


def test_rv07_k2_reactivation_does_not_resend_cancel(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.broker.behavior = FakeBehavior.REJECT          # 브로커가 취소를 거부 → 주문은 열린 채
    first = _k2(env)
    env.executor.process_events()
    assert _cancels(env) == 1
    for _ in range(3):                                 # 매 tick check_daily_loss (K2 유지)
        assert _k2(env) == first
        env.executor.process_events()
    assert _cancels(env) == 1                          # 자동 재시도 없음 (CLAUDE.md §8-7)


def test_rv07_same_halt_activate_again_does_not_cancel_again_k1(env: Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-B", "ob")
    env.broker.behavior = FakeBehavior.REJECT
    env.halts.activate(HaltType.K1, "u", "킬")
    env.halts.activate(HaltType.K1, "u", "킬 다시")
    assert _cancels(env) == 1


# --- RV-02 K5 재개 조건 broker_connected (연결 경로별: 조회·실시간) --------------------------------------

def _reconciler(env: Env):  # type: ignore[no-untyped-def]
    from trading_system.reconciliation.reconciler import Reconciler
    from trading_system.storage.alerts import AuditAlertSink
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock))


def test_rv02_k5_from_query_failure_released_by_successful_reconcile(env: Env) -> None:
    rec = _reconciler(env)
    env.broker.disconnected = True
    assert rec.run(ACCOUNT, "PERIODIC").result == "QUERY_FAILED"
    env.halts.activate(HaltType.K5, "recovery", "조회 실패", ACCOUNT)
    assert rec.run(ACCOUNT, "PERIODIC").result == "QUERY_FAILED"
    assert "K5" in env.repo.active_halts(ACCOUNT)                 # 조회 실패는 연결 증거가 아니다
    env.broker.disconnected = False
    env.clock.advance(timedelta(seconds=1))
    assert rec.run(ACCOUNT, "PERIODIC").result == "MATCH"
    assert "K5" not in env.repo.active_halts(ACCOUNT)             # 조회 성공(연결) + 대조 일치 → 자동 해제


def test_rv02_k5_realtime_needs_realtime_up_and_reconcile_after_it(env: Env) -> None:
    from trading_system.halt.manager import K5_LINK_REALTIME
    rec = _reconciler(env)
    env.halts.activate(HaltType.K5, "realtime", "실시간 끊김", ACCOUNT)
    env.clock.advance(timedelta(seconds=1))
    assert rec.run(ACCOUNT, "PERIODIC").result == "MATCH"
    assert "K5" in env.repo.active_halts(ACCOUNT)                 # 조회는 되지만 실시간은 아직 끊김
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REALTIME, True, "재접속")
    assert "K5" in env.repo.active_halts(ACCOUNT)                 # 재접속 전 대조는 누락 통보를 못 본다
    env.clock.advance(timedelta(seconds=1))
    rec.run(ACCOUNT, "RECONNECT")
    assert "K5" not in env.repo.active_halts(ACCOUNT)


def test_rv02_k5_both_links_down_needs_both(env: Env) -> None:
    from trading_system.halt.manager import K5_LINK_REALTIME
    rec = _reconciler(env)
    env.halts.activate(HaltType.K5, "realtime", "실시간 끊김", ACCOUNT)
    env.halts.activate(HaltType.K5, "broker", "토큰 발급 실패", ACCOUNT)
    env.clock.advance(timedelta(seconds=1))
    env.halts.report_condition(HaltType.K5, ACCOUNT, K5_LINK_REALTIME, True, "재접속")
    env.clock.advance(timedelta(seconds=1))
    env.broker.disconnected = True
    rec.run(ACCOUNT, "PERIODIC")
    assert "K5" in env.repo.active_halts(ACCOUNT)                 # 조회 경로는 아직 실패
    env.broker.disconnected = False
    env.clock.advance(timedelta(seconds=1))
    rec.run(ACCOUNT, "PERIODIC")
    assert "K5" not in env.repo.active_halts(ACCOUNT)


# --- RV-03 K7 재개 조건 storage_ok: 재시작 복구 점검의 무결성·쓰기 성공으로 보고 ---------------------------

def test_rv03_k7_storage_ok_reported_by_recovery_then_local_resume(env: Env) -> None:
    from trading_system.halt.manager import Channel
    from trading_system.reconciliation.reconciler import RecoveryChecker
    env.halts.activate(HaltType.K7, "executor", "저장 실패", ACCOUNT)
    assert not env.halts.resume(HaltType.K7, None, Channel.LOCAL, "u").ok
    rep = RecoveryChecker(env.repo, _reconciler(env), env.halts, lambda: True).check(ACCOUNT)
    assert rep.ok, rep.failures
    assert "K7" in env.repo.active_halts(ACCOUNT)                 # 자동 해제 아님 — 사용자 재개
    assert not env.halts.resume(HaltType.K7, None, Channel.REMOTE, "u").ok
    assert env.halts.resume(HaltType.K7, None, Channel.LOCAL, "u").ok


def test_rv03_k7_not_reported_ok_when_integrity_fails(env: Env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    from trading_system.halt.manager import Channel
    from trading_system.reconciliation.reconciler import RecoveryChecker
    env.halts.activate(HaltType.K7, "executor", "저장 실패", ACCOUNT)
    monkeypatch.setattr(env.repo, "integrity_ok", lambda: False)
    rep = RecoveryChecker(env.repo, _reconciler(env), env.halts, lambda: True).check(ACCOUNT)
    assert not rep.ok
    assert not env.halts.resume(HaltType.K7, None, Channel.LOCAL, "u").ok


# --- RV-20 전략 도구의 STOPPED 전이가 K9로 이어짐 (운영 명령으로 엔진에 맡김) ----------------------------

def _set_state(w: World, state: str, reason: str | None) -> None:
    row = w.repo.strategy_state("t-cross", 1, "M1")
    with w.repo.tx():
        w.repo.upsert_strategy_state("t-cross", 1, "M1", state, reason, "PAPER", row["spec_hash"], w.clock.now())


def _k9_rows(w: World) -> list[dict]:
    return [r for r in w.repo.halt_rows(None) if r["halt_type"] == "K9"]


def test_rv20_strategy_sync_command_activates_and_releases_k9(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.engine.start()
        _set_state(w, "STOPPED", "USER")                    # 도구(별도 프로세스)가 저장소에 쓴 전이
        w.command("STRATEGY_SYNC", {"owner": "t-cross:1"})
        w.engine.tick()
        assert _cmd_rows(w.repo)[-1]["status"] == "DONE", _cmd_rows(w.repo)[-1]
        [k9] = _k9_rows(w)
        assert k9["scope_owner"] == "t-cross:1" and k9["variant"] == "USER"
        _set_state(w, "PAPER", None)                        # 중지 해제 전이(승인 A-ID는 도구가 기록)
        w.command("STRATEGY_SYNC", {"owner": "t-cross:1"})
        w.engine.tick()
        assert _cmd_rows(w.repo)[-1]["status"] == "DONE", _cmd_rows(w.repo)[-1]
        assert _k9_rows(w) == []
    finally:
        w.close()


def test_rv20_strategy_sync_is_local_only(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.engine.start()
        _set_state(w, "STOPPED", "USER")
        with w.repo.tx():
            w.repo.insert_command("remote-1", "REMOTE", "x", "STRATEGY_SYNC", {"owner": "t-cross:1"}, w.clock.now())
        w.engine.tick()
        assert _cmd_rows(w.repo)[-1]["status"] == "REJECTED"
    finally:
        w.close()


def test_rv20_strategy_tool_stop_queues_sync_command(tmp_path: Path) -> None:
    from tests.scenario.test_t016_paper0 import tool_root
    from trading_system.app.strategy_tool import strategy_main
    from trading_system.domain.enums import Mode
    from trading_system.storage.repository import Repository
    root = tool_root(tmp_path)
    base = ["--mode", "PAPER", "--root", str(root)]
    assert strategy_main(["register", *base]) == 0
    (root / "var/backtest/x").mkdir(parents=True, exist_ok=True)   # 증거 파일 실재 필요 (V-113)
    (root / "var/backtest/x/report.md").write_text("# report", encoding="utf-8")
    assert strategy_main(["transition", *base, "paper0-verify", "1", "BACKTEST_PASSED", "--approval", "A-901",
                          "--evidence", "var/backtest/x/report.md"]) == 0
    assert strategy_main(["transition", *base, "paper0-verify", "1", "PAPER", "--approval", "A-902"]) == 0
    assert strategy_main(["transition", *base, "paper0-verify", "1", "STOPPED", "--approval", "A-903",
                          "--stop-reason", "USER"]) == 0
    repo = Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)
    try:
        cmds = [c for c in repo.pending_commands() if c["command"] == "STRATEGY_SYNC"]
        assert cmds and all(c["channel"] == "LOCAL" for c in cmds)
        assert '"paper0-verify:1"' in cmds[-1]["args"]
    finally:
        repo.close()


# --- RV-24 대조 불일치 시 통보 반영 + 재조회 1회 후에만 K4 (01 장애 정책 "잔고 불일치" 행) ---------------------

def _reconciler_drain(env: Env):  # type: ignore[no-untyped-def]
    from trading_system.reconciliation.reconciler import Reconciler
    from trading_system.storage.alerts import AuditAlertSink
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock),
                      drain_events=env.executor.process_events)


def test_rv24_fill_arriving_during_query_is_not_k4(env: Env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    no = env.repo.broker_order(_limit_buy(env, "TST-B", "ob")).order_no
    real = env.broker.query_orders
    calls = []

    def orders_then_fill(account: str):  # type: ignore[no-untyped-def]
        out = real(account)
        if not calls:
            env.broker.fill(no)                       # 잔고 조회 뒤·체결 조회 전에 체결 (통보는 아직 미반영)
        calls.append(1)
        return out

    monkeypatch.setattr(env.broker, "query_orders", orders_then_fill)
    r = _reconciler_drain(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH", env.repo.open_recon_items(ACCOUNT)
    assert "K4" not in env.repo.active_halts(ACCOUNT)
    assert len(calls) == 2                            # 재조회 1회


def test_rv24_persistent_mismatch_k4_after_one_requery(env: Env) -> None:
    env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("1"))
    before = env.broker.queries
    r = _reconciler_drain(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MISMATCH"
    assert "K4" in env.repo.active_halts(ACCOUNT)
    assert env.broker.queries - before == 6           # 조회 3종 × (처음 + 재조회 1회)


def test_rv24_requery_failure_keeps_first_mismatch(env: Env, monkeypatch) -> None:  # type: ignore[no-untyped-def]
    env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("1"))
    rec = _reconciler_drain(env)
    real = env.broker.query_positions
    n = []

    def fail_second(account: str):  # type: ignore[no-untyped-def]
        n.append(1)
        if len(n) == 2:
            env.broker.query_failures = 3
        return real(account)

    monkeypatch.setattr(env.broker, "query_positions", fail_second)
    assert rec.run(ACCOUNT, "PERIODIC").result == "MISMATCH"   # 재조회 실패는 일치 증거가 아니다
    assert "K4" in env.repo.active_halts(ACCOUNT)


# --- RV-23 현금 조회 신선도 + 미반영 당일 매수 체결 보수 반영 (A-043 (3)) ------------------------------------

def _buy_fill(env: Env, qty: str = "100") -> None:
    from tests.scenario.conftest import make_signal
    from trading_system.domain.enums import RequestType
    from trading_system.domain.models import GateRequest
    env.broker.behavior = FakeBehavior.FILL
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY, instrument="TST-A", quantity=qty,
                                                            evaluation="rv23")))
    assert d.approved is not None, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()


class _Inner:
    def __init__(self, view) -> None:  # type: ignore[no-untyped-def]
        self.view = view

    def cash(self, account: str):  # type: ignore[no-untyped-def]
        return self.view

    def daily_risk(self, account: str):  # type: ignore[no-untyped-def]
        return None


def _rv23(env: Env):  # type: ignore[no-untyped-def]
    from trading_system.domain.market import CashView, CostRates
    from trading_system.monitoring.watchers import ConservativeCashFacts, DailyRiskTracker
    rates = CostRates(Decimal("0.00015"), Decimal("0.002"))
    tracker = DailyRiskTracker(env.repo, lambda i: Decimal("10000"), clock=env.clock, cost_rates=rates)
    inner = _Inner(CashView(Decimal("10000000"), Decimal("10000000")))
    facts = ConservativeCashFacts(inner, tracker, env.clock, timedelta(minutes=3))
    return tracker, facts


def test_rv23_g8_cash_subtracts_same_day_buy_fills_and_post_snapshot_fills(env: Env) -> None:
    tracker, facts = _rv23(env)
    tracker.update_cash(ACCOUNT, Decimal("10000000"))          # 현금 조회 시각 T0
    env.clock.advance(timedelta(seconds=10))
    _buy_fill(env)                                             # 100주 × 10,000 = 1,000,000 + 비용 150
    v = facts.cash(ACCOUNT)
    assert v.deposit == Decimal("8999850")                     # 예수금: 당일 매수 체결 전액 차감(T+2 미반영 가정)
    assert v.broker_orderable == Decimal("8999850")            # 주문가능: 조회 이후 체결만 차감
    env.clock.advance(timedelta(seconds=10))
    tracker.update_cash(ACCOUNT, Decimal("10000000"))          # 체결 뒤 새 조회
    v = facts.cash(ACCOUNT)
    assert v.broker_orderable == Decimal("10000000") and v.deposit == Decimal("8999850")


def test_rv23_stale_or_unknown_cash_snapshot_is_unknown(env: Env) -> None:
    tracker, facts = _rv23(env)
    assert facts.cash(ACCOUNT) is None                         # 조회 기록 없음
    tracker.update_cash(ACCOUNT, Decimal("10000000"))
    assert facts.cash(ACCOUNT) is not None
    env.clock.advance(timedelta(minutes=3, seconds=1))
    assert facts.cash(ACCOUNT) is None                         # 오래된 조회 → 모름 → G8 위험 확대 거부


def test_rv23_wired_cash_becomes_unknown_when_refresh_fails(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.engine.start()
        w.resume_k8()
        assert w.engine.gate._in.account.cash(APP_ACCOUNT) is not None  # noqa: SLF001
        w.clock._now = w.clock._now + timedelta(minutes=11)  # noqa: SLF001
        w.broker.query_failures = 100                           # 조회 실패 → 현금 갱신 없음
        w.engine.tick()
        assert w.engine.gate._in.account.cash(APP_ACCOUNT) is None  # noqa: SLF001
    finally:
        w.broker.query_failures = 0
        w.close()


def test_rv23_daily_pnl_does_not_hide_loss_from_unreflected_buy(env: Env) -> None:
    tracker, _ = _rv23(env)
    assert tracker.start_day(ACCOUNT, Decimal("10000000")) == Decimal("10000000")
    _buy_fill(env)
    tracker.update_cash(ACCOUNT, Decimal("10000000"))          # entr가 당일 매수를 반영하지 않은 경우
    d = tracker.daily_risk(ACCOUNT)
    assert d.unrealized_change == Decimal("-150")              # 비용만큼 손실 (보정 없으면 +1,000,000으로 보임)
