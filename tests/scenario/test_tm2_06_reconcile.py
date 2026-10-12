"""T-M2-06 대조 일반화 (CP-024 D-099·D-103·D-109·D-111·D-118, 승인 A-066·A-071).

검증: V-143(자산별 잔고 free+locked 대조, USDT 기대값 대조 — 거래소 보고 금액으로 오탐 없음, 범위 밖 자산은 기록·정보 알림),
V-130(K4 경로: 대조에서 기초·USDT 외 수수료 자산 체결 → K4), V-144(리셋 모의 → K4, 자동 기준선 재설정 없음, "리셋 확인"
절차 뒤에만 재개), V-153(대조 시점: 시작·60초 주기·사용자 스트림 재연결 후·UTC 일 경계 1회 + 일일 확정·백업, 1분 넘게 열린
주문 알림), V-155(대조기 부분: 결과 불명 의도의 브로커 주문은 전송 후 24시간 이내 보류 — UTC 자정 통과만으로 K4 아님,
24시간 초과면 보류하지 않음), V-152(M1 대조 일정·보류 기준 유지 — 기존 M1 시험이 담당).
모든 값은 가상이다(tests/support/m2_fakes.py). 네트워크 없음. 매 시험 끝에 불변 조건을 검사한다 (04 D-069).
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.invariants import check_invariants
from tests.support.m2_fakes import (
    ACCOUNT, PAIRS, QUOTE_ASSET, RULES, T0, M2Env, build_m2_env, ioc_buy_price,
)
from trading_system.broker.fake.broker import FakeBehavior, FakeSpotFees
from trading_system.domain.enums import GateResult, HaltType, Mode, OrderType, RequestType
from trading_system.domain.models import GateRequest, Signal
from trading_system.halt.manager import Channel
from trading_system.monitoring.spot_cash import QuoteBalance
from trading_system.reconciliation.spot import (
    DAY_BOUNDARY, PERIODIC, STARTUP, STREAM_RECONNECT, ResetError, SpotItemKind, SpotReconcileCycle,
    SpotReconcileSchedule, SpotReconciler, confirm_reset,
)
from trading_system.storage.alerts import AuditAlertSink

STRATEGY = "tm206-fixed"  # 테스트 전용 고정 전략 (실제 매매전략 아님)
D = Decimal


@pytest.fixture
def env(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path, spot_cash=True)
    f = e.account
    f.set_baseline(ACCOUNT, D("10000"), e.clock.now())
    f.update_balance(ACCOUNT, QuoteBalance(D("10000"), D("0"), e.clock.now()))
    assert f.start_day(ACCOUNT) is not None
    e.clock.advance(timedelta(seconds=1))   # 기준선 시각 뒤 체결 (spot_cash [제안](2) 경계 회피)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def signal(env: M2Env, kind: RequestType = RequestType.ENTRY, instrument: str = "TSTA-USDT",
           quantity: str = "0.1000", evaluation: str = "eval-1") -> Signal:
    rule = RULES[instrument]
    buying = kind in (RequestType.ENTRY, RequestType.INCREASE)
    px = ioc_buy_price(rule.last, rule.tick_size) if buying else rule.last
    now = env.clock.now()
    return Signal(strategy_id=STRATEGY, strategy_version=1, evaluation_id=evaluation, account=ACCOUNT,
                  instrument=instrument, rule_id="R1", kind=kind, quantity=D(quantity),
                  order_type=OrderType.LIMIT if buying else OrderType.MARKET, price=px,
                  as_of=now - timedelta(minutes=1), valid_until=now + timedelta(hours=30))


def run(env: M2Env, sig: Signal):  # type: ignore[no-untyped-def]
    d = env.gate.decide(GateRequest.from_signal(sig))
    assert d.result is GateResult.APPROVED, d.reason
    env.executor.submit(d.approved)
    env.executor.process_events()
    return d.approved.intent_id


def reconciler(env: M2Env) -> SpotReconciler:
    return SpotReconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock),
                          universe=PAIRS, quote_asset=QUOTE_ASSET, expected_quote=env.account.expected_quote,
                          drain_events=env.executor.process_events)


def items(env: M2Env) -> list[str]:
    return [i["kind"] for i in env.repo.open_recon_items(ACCOUNT)]


def alerts(env: M2Env) -> list[tuple[str, str]]:
    return [(json.loads(e["summary"])["level"], json.loads(e["summary"])["message"])
            for e in env.repo.audit_events() if e["kind"] == "ALERT"]


def k(env: M2Env, halt: HaltType) -> bool:
    return halt.value in env.repo.active_halts(ACCOUNT)


# --- V-143 자산별·USDT 대조 ---------------------------------------------------------------------------------

def test_v143_buy_sell_reconcile_match_with_asset_rows(env: M2Env) -> None:
    run(env, signal(env))
    env.clock.advance(timedelta(minutes=1))
    run(env, signal(env, RequestType.REDUCE, quantity="0.0500", evaluation="r1"))
    r = reconciler(env).run(ACCOUNT, PERIODIC)
    assert r.result == "MATCH", items(env)
    rows = {x["asset"]: x for x in env.repo.select("SELECT * FROM recon_asset WHERE recon_id=?", (r.recon_id,))}
    assert D(rows["TSTA"]["internal"]) == D("0.0499") and D(rows["TSTA"]["broker_total"]) == D("0.0499")
    assert D(rows[QUOTE_ASSET]["internal"]) == D(rows[QUOTE_ASSET]["broker_total"])
    assert rows["TSTA"]["in_scope"] == 1 and not k(env, HaltType.K4)


def test_v143_reported_amount_avoids_rounding_false_k4(env: M2Env) -> None:
    """fills만(임시 price × qty)과 거래소 보고 금액(Y)이 반올림으로 다를 때: 보고 금액이 반영된 뒤 대조 → 일치."""
    env.broker.provisional_quotes = True
    env.broker.quote_report = lambda qty, px: qty * px - D("0.00000123")
    run(env, signal(env))
    env.broker.confirm_quotes()   # 사용자 스트림 Y 도착(아직 반영 전) — 대조가 먼저 통보를 반영한다
    assert reconciler(env).run(ACCOUNT, PERIODIC).result == "MATCH", items(env)
    assert not k(env, HaltType.K4)


def test_v143_usdt_mismatch_raises_k4(env: M2Env) -> None:
    run(env, signal(env))
    env.broker.adjust(QUOTE_ASSET, D("-5"))   # 외부 출금(가상)
    r = reconciler(env).run(ACCOUNT, PERIODIC)
    assert r.result == "MISMATCH" and SpotItemKind.QUOTE_CASH_MISMATCH in items(env)
    assert k(env, HaltType.K4)


def test_v143_base_asset_mismatch_raises_k4(env: M2Env) -> None:
    run(env, signal(env))
    env.broker.adjust("TSTA", D("-0.0100"))   # 거래소 앱 직접 매도(가상)
    assert reconciler(env).run(ACCOUNT, PERIODIC).result == "MISMATCH"
    assert "QTY_MISMATCH" in items(env) and k(env, HaltType.K4)


def test_v143_out_of_scope_asset_change_is_info_only(env: M2Env) -> None:
    env.broker.grant["TSTX"] = D("1")          # 유니버스 밖 자산 (가상)
    rec = reconciler(env)
    assert rec.run(ACCOUNT, STARTUP).result == "MATCH"
    snap = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "ASSET_SNAPSHOT"]
    assert snap and snap[-1]["assets"]["TSTX"] == "1"
    env.broker.adjust("TSTX", D("-0.25"))
    assert rec.run(ACCOUNT, PERIODIC).result == "MATCH"   # 기록만 — 불일치 아님
    assert not k(env, HaltType.K4)
    info = [m for lv, m in alerts(env) if lv == "INFO" and "범위 밖" in m]
    assert len(info) == 1 and "TSTX" in info[0]
    rec.run(ACCOUNT, PERIODIC)
    assert len([m for lv, m in alerts(env) if lv == "INFO" and "범위 밖" in m]) == 1   # 같은 변화는 한 번만


def test_v143_missing_baseline_is_mismatch(tmp_path: Path) -> None:
    e = build_m2_env(tmp_path, spot_cash=True)
    try:
        r = reconciler(e).run(ACCOUNT, STARTUP)
        assert r.result == "MISMATCH" and SpotItemKind.QUOTE_BASELINE_MISSING in items(e)
        assert k(e, HaltType.K4)
    finally:
        e.repo.close()


def test_v130_unapproved_fee_asset_found_by_reconcile_k4(env: M2Env) -> None:
    """실시간 통보를 놓친 체결의 수수료 자산이 기초·USDT 외(가상 TSTFEE) → 대조에서 K4 (D-101)."""
    env.broker.spot_fees = FakeSpotFees(rate=D("0.001"), pairs=env.broker.spot_fees.pairs, asset_override="TSTFEE")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    run(env, signal(env))
    [no] = env.broker.open_order_nos()
    env.broker.fill(no)
    env.broker.drop_events()
    assert reconciler(env).run(ACCOUNT, PERIODIC).result == "MISMATCH"
    assert SpotItemKind.FEE_ASSET_UNAPPROVED in items(env) and k(env, HaltType.K4)


# --- V-144 테스트넷 리셋 -------------------------------------------------------------------------------------

def test_v144_reset_k4_no_auto_rebaseline_then_reset_confirm(env: M2Env) -> None:
    run(env, signal(env))
    baseline = env.account.baseline(ACCOUNT)
    env.broker.reset({QUOTE_ASSET: D("5000"), "TSTB": D("2.0")})   # 잔고 재지급 (가상)
    rec = reconciler(env)
    assert rec.run(ACCOUNT, PERIODIC).result == "MISMATCH" and k(env, HaltType.K4)
    assert env.account.baseline(ACCOUNT) == baseline                # 자동 기준선 재설정 없음
    assert rec.run(ACCOUNT, PERIODIC).result == "MISMATCH"           # 다시 대조해도 그대로
    assert not env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "user").ok   # 재개 불가

    env.clock.advance(timedelta(minutes=5))   # 사람이 확인한 뒤 (기준선 시각 = 리셋 확인 시각)
    report = confirm_reset(env.repo, env.clock, env.broker, ACCOUNT, universe=PAIRS, quote_asset=QUOTE_ASSET,
                           set_baseline=env.account.set_baseline, operator="user")
    assert report.resolved_items and report.quote_baseline == D("5000")
    kinds = [e["kind"] for e in env.repo.audit_events()]
    assert "TESTNET_RESET_CONFIRMED" in kinds and "PAPER_PERIOD_RESTART" in kinds
    assert env.account.baseline(ACCOUNT) == D("5000")
    assert env.repo.position(ACCOUNT, "TSTA-USDT").quantity == 0
    assert env.repo.position(ACCOUNT, "TSTB-USDT").quantity == D("2.0")
    own = env.repo.active_ownership(ACCOUNT, "TSTB-USDT")
    assert own is not None and own.owner == "manual"                # 재지급된 기초 자산 = 수동 보유 (전략 신호 금지)
    assert env.repo.active_ownership(ACCOUNT, "TSTA-USDT") is None
    assert rec.run(ACCOUNT, PERIODIC).result == "MATCH", items(env)
    assert env.halts.resume(HaltType.K4, ACCOUNT, Channel.LOCAL, "user").ok


def test_v144_reset_confirm_refused_with_active_intent(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.UNKNOWN
    run(env, signal(env))   # 결과 불명 의도가 남음
    with pytest.raises(ResetError):
        confirm_reset(env.repo, env.clock, env.broker, ACCOUNT, universe=PAIRS, quote_asset=QUOTE_ASSET,
                      set_baseline=env.account.set_baseline, operator="user")
    assert "TESTNET_RESET_CONFIRMED" not in [e["kind"] for e in env.repo.audit_events()]


# --- V-155 대조기: 결과 불명 보류는 전송 후 24시간 (UTC 자정 무관) -------------------------------------------------

def test_v155_unknown_order_deferred_across_utc_midnight(env: M2Env) -> None:
    env.clock.advance(datetime(2026, 10, 7, 23, 49, 0, tzinfo=UTC) - env.clock.now())
    env.account.update_balance(ACCOUNT, QuoteBalance(D("10000"), D("0"), env.clock.now()))
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    run(env, signal(env))
    env.clock.advance(timedelta(minutes=12))      # UTC 00:01 (다음 날)
    rec = reconciler(env)
    r = rec.run(ACCOUNT, PERIODIC)
    assert SpotItemKind.UNKNOWN_BROKER_ORDER not in items(env)   # 보류 — K4 아님
    summary = json.loads(env.repo.select("SELECT summary FROM reconciliation WHERE recon_id=?",
                                         (r.recon_id,))[0]["summary"])
    assert summary["deferred_unknown"]
    env.clock.advance(timedelta(hours=24))        # 전송 후 24시간 초과 → 보류하지 않음
    rec.run(ACCOUNT, PERIODIC)
    assert SpotItemKind.UNKNOWN_BROKER_ORDER in items(env) and k(env, HaltType.K4)


# --- V-153 대조 시점·일 경계·1분 넘게 열린 주문 -------------------------------------------------------------------

def test_v153_schedule_startup_periodic_reconnect_and_day_boundary_once() -> None:
    s = SpotReconcileSchedule()
    t = datetime(2026, 10, 7, 23, 50, 0, tzinfo=UTC)
    assert s.due(t) == [STARTUP]
    s.mark(STARTUP, t)
    assert s.due(t + timedelta(seconds=30)) == []
    assert s.due(t + timedelta(seconds=60)) == [PERIODIC]
    s.mark(PERIODIC, t + timedelta(seconds=60))
    assert s.due(t + timedelta(seconds=61), stream_reconnected=True) == [STREAM_RECONNECT]
    boundary = datetime(2026, 10, 8, 0, 0, 5, tzinfo=UTC)
    assert DAY_BOUNDARY in s.due(boundary)
    s.mark(DAY_BOUNDARY, boundary)
    assert DAY_BOUNDARY not in s.due(boundary + timedelta(hours=3))
    kst_midnight = datetime(2026, 10, 8, 15, 0, 5, tzinfo=UTC)   # KST 자정 — 경계 아님
    assert DAY_BOUNDARY not in s.due(kst_midnight)
    assert DAY_BOUNDARY in s.due(datetime(2026, 10, 9, 0, 0, 1, tzinfo=UTC))


def test_v153_cycle_day_boundary_runs_close_and_backup_once(env: M2Env) -> None:
    closed: list[tuple[str, str]] = []
    cycle = SpotReconcileCycle(reconciler(env), ACCOUNT, SpotReconcileSchedule(),
                               on_day_close=lambda trade_date, res: closed.append((trade_date, res.result)))
    assert cycle.tick() == [STARTUP]
    env.clock.advance(datetime(2026, 10, 8, 0, 0, 3, tzinfo=UTC) - env.clock.now())
    ran = cycle.tick()
    assert DAY_BOUNDARY in ran and closed == [("2026-10-07", "MATCH")]
    env.clock.advance(timedelta(minutes=2))
    assert DAY_BOUNDARY not in cycle.tick() and len(closed) == 1
    assert cycle.tick(stream_reconnected=True) == [STREAM_RECONNECT]
    triggers = [json.loads(e["summary"])["trigger"] for e in env.repo.audit_events() if e["kind"] == "RECONCILE"]
    assert triggers.count(DAY_BOUNDARY) == 1 and STREAM_RECONNECT in triggers


def test_v153_order_open_over_one_minute_alerts_once(env: M2Env) -> None:
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY   # IOC가 아니면 열린 채 남는다(가상) — 정상은 IOC라 없음
    run(env, signal(env))
    rec = reconciler(env)
    rec.run(ACCOUNT, PERIODIC)
    assert not [m for _, m in alerts(env) if "1분" in m]
    env.clock.advance(timedelta(seconds=61))
    rec.run(ACCOUNT, PERIODIC)
    rec.run(ACCOUNT, PERIODIC)
    warn = [m for lv, m in alerts(env) if lv == "WARNING" and "1분" in m]
    assert len(warn) == 1
