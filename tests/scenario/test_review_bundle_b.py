"""묶음 B 적대적 검토(2026-10-03)에서 확정된 결함의 회귀 시험. 모든 값은 가상이다.

B-1  외부 주문 인정이 해소한 날짜를 거래일로 기록 → 다음 날 우리 주문번호와 충돌·실행기 차단
B-2  수량 차이 "외부 거래 반영"이 대조 이후 바뀐 원장을 덮어씀 / 누락 체결과 이중 반영
B-3  같은 전략 ID·버전을 다른 명세·파라미터로 재등록 → 승인 상태 유지한 채 전략 한도 교체 (D-011 우회)
B-4  재시작 복구 판정이 전송 중(SENT)·전송 대기 의도를 보지 않음
B-5  일봉이 오래 끊겨도 품질 통과 (공급 중단)
B-6  백테스트 스냅샷에 보유 거래일 수 미기재(항상 0)
B-8  달력 특수 개장일 날짜를 문자열로 적으면 조용히 무시
B-9  마스킹 누락 (api_key·Authorization·signature·URL token·다른 계좌 형식)
B-10 중지(교체) → 폐기 후에도 K9 사유가 교체로 남음, 소유 보유가 남은 채 폐기
B-11 누락 체결 반영이 실제로 거부돼도 항목이 해소됨으로 기록
B-12 열린 불일치 항목이 남아 있는데 대조 일치로 K4 재개 조건 충족
B-13 마이그레이션 SQL을 ';'로 나눔(트리거 본문 깨짐), meta 없는 파일의 복원 시험 예외
B-14 운영 로그 파일 날짜가 UTC
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, T0, Env, make_signal
from tests.support.gate_fakes import scenario_limits
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, cross_spec
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.config.strategy_registry import StrategyRegistry
from trading_system.domain.clock import KST
from trading_system.domain.enums import GateResult, HaltType, IntentState, Mode, RequestType
from trading_system.domain.models import GateRequest
from trading_system.domain.strategy import StopReason, StrategyLimits, StrategyState
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.marketdata.errors import MarketDataError
from trading_system.marketdata.quality import RawDaily
from trading_system.marketdata.store import MarketDataStore
from trading_system.ops.reconcile import ReconcileResolver, ResolveError
from trading_system.reconciliation.reconciler import ItemKind, Reconciler, RecoveryChecker
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.jsonlog import JsonLogger, Masker
from trading_system.storage.maintenance import migrate, restore_test, schema_version_of
from trading_system.storage.repository import Repository


def rec(env: Env) -> Reconciler:
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock))


def res(env: Env) -> ReconcileResolver:
    return ReconcileResolver(env.repo, env.executor, env.clock)


def enter(env: Env, kind: RequestType = RequestType.ENTRY, inst: str = "TST-A", qty: str = "10", ev: str = "e1"):  # type: ignore[no-untyped-def]
    now = env.clock.now()
    s = make_signal(kind, instrument=inst, quantity=qty, evaluation=ev)
    s = replace(s, as_of=now - timedelta(minutes=1), valid_until=now + timedelta(minutes=30))
    d = env.gate.decide(GateRequest.from_signal(s))
    if d.approved is not None:
        env.executor.submit(d.approved)
        env.executor.process_events()
    return d


def item(env: Env, kind: str) -> dict:
    return next(i for i in env.repo.open_recon_items(ACCOUNT) if i["kind"] == kind)


# --- B-1 ----------------------------------------------------------------------------------------

def test_b1_external_order_recorded_on_its_trade_date(env: Env) -> None:
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("3"))
    rec(env).run(ACCOUNT, "PERIODIC")
    it = item(env, ItemKind.UNKNOWN_BROKER_ORDER)
    env.clock.advance(timedelta(days=1))           # 다음 날 해소
    res(env).resolve(it["item_id"], "EXTERNAL", "u")
    keys = [r["order_key"] for r in env.repo.select("SELECT order_key FROM broker_order WHERE origin='EXTERNAL'")]
    assert keys == [f"{ACCOUNT}|{T0.astimezone(KST).date().isoformat()}|{no}"]
    env.broker.new_day()                           # 다음 날 번호 재시작 → 우리 주문 0000001
    env.halts.report_condition(HaltType.K4, ACCOUNT, "reconciled", True)
    env.halts.resume(HaltType.K4, ACCOUNT, "LOCAL", "u")
    d = enter(env, inst="TST-A", ev="next")
    assert d.result is GateResult.APPROVED
    assert env.repo.intent(d.approved.intent_id).state is IntentState.FILLED  # 충돌·차단 없음


# --- B-2 ----------------------------------------------------------------------------------------

def test_b2_external_resolution_refused_when_ledger_changed_since_reconcile(env: Env) -> None:
    enter(env, qty="10")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    inc = enter(env, RequestType.INCREASE, qty="5", ev="inc")
    ext = env.broker.add_external_order(ACCOUNT, "TST-A", "SELL", Decimal("4"))
    env.broker.fill(ext)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    order = env.repo.broker_order(env.repo.intent(inc.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no)
    env.executor.process_events()                  # 대조 뒤 내부 원장이 바뀜 (10 → 15)
    with pytest.raises(ResolveError, match="재대조"):
        res(env).resolve(item(env, ItemKind.QTY_MISMATCH)["item_id"], "EXTERNAL", "u")
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("15")


def test_b2_external_resolution_refused_while_missing_fill_open(env: Env) -> None:
    enter(env, qty="10")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    red = enter(env, RequestType.REDUCE, qty="4", ev="red")
    order = env.repo.broker_order(env.repo.intent(red.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    with pytest.raises(ResolveError, match="누락 체결"):
        res(env).resolve(item(env, ItemKind.QTY_MISMATCH)["item_id"], "EXTERNAL", "u")
    res(env).resolve(item(env, ItemKind.MISSING_FILL)["item_id"], "APPLY_FILL", "u")
    assert env.repo.position(ACCOUNT, "TST-A").quantity == Decimal("6")


# --- B-3 ----------------------------------------------------------------------------------------

def test_b3_same_version_with_different_spec_or_params_refused(env: Env) -> None:
    reg = StrategyRegistry(env.repo, env.clock, env.halts)
    spec = cross_spec(limits=StrategyLimits(l2_max_symbol_ratio=Decimal("0.05")))
    assert reg.register(spec, dict(PARAMS), CAPS_ALL_SUPPORTED, scenario_limits(), Mode.PAPER).ok
    other = reg.register(cross_spec(), dict(PARAMS), CAPS_ALL_SUPPORTED, scenario_limits(), Mode.PAPER)
    assert not other.ok and any("버전" in p for p in other.problems)
    params2 = {**PARAMS, "qty": 20}
    assert not reg.register(spec, params2, CAPS_ALL_SUPPORTED, scenario_limits(), Mode.PAPER).ok
    assert reg.register(spec, dict(PARAMS), CAPS_ALL_SUPPORTED, scenario_limits(), Mode.PAPER).ok  # 같은 명세 재등록은 허용
    assert reg.strategy_limits("t-cross:1").l2_max_symbol_ratio == Decimal("0.05")


# --- B-4 ----------------------------------------------------------------------------------------

def test_b4_recovery_not_ok_while_intents_sent_or_pending(env: Env) -> None:
    d = env.gate.decide(GateRequest.from_signal(make_signal(RequestType.ENTRY)))
    with env.repo.tx():
        env.repo.insert_attempt(d.approved.intent_id, env.clock.now())
        env.repo.set_intent_state(d.approved.intent_id, IntentState.SENT)
    env.halts.on_start()
    rep = RecoveryChecker(env.repo, rec(env), env.halts, lambda: True).check(ACCOUNT)
    assert not rep.ok and any("전송" in f for f in rep.failures)


# --- B-5·B-6 -------------------------------------------------------------------------------------

CAL = KrxCalendar({2026: parse_calendar({"market": "KRX", "year": 2026, "source": "가상", "source_version": "t"}, 2026)})


def _days(a: date, b: date) -> list[date]:
    out, d = [], a
    while d <= b:
        if CAL.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def test_b5_stale_daily_data_is_not_ok() -> None:
    from trading_system.domain.market import InstrumentInfo, InstrumentKind
    s = MarketDataStore(CAL, None)
    s.register(InstrumentInfo("TST-A", InstrumentKind.STOCK, Decimal("1"), Decimal("1")))
    rows = [RawDaily(d, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))
            for d in _days(date(2026, 8, 3), date(2026, 9, 10))]
    s.update_daily("TST-A", rows)
    snap = s.snapshot("ev", "M1", ACCOUNT, datetime(2026, 10, 2, 16, 0, tzinfo=KST), Decimal("1"), ["TST-A"],
                      lambda i: None, lambda i: False)
    assert not snap.view("TST-A").data_ok
    fresh = rows + [RawDaily(d, Decimal("100"), Decimal("101"), Decimal("99"), Decimal("100"), Decimal("1"))
                    for d in _days(date(2026, 9, 11), date(2026, 10, 2))]
    s.update_daily("TST-A", fresh)
    snap2 = s.snapshot("ev", "M1", ACCOUNT, datetime(2026, 10, 2, 16, 0, tzinfo=KST), Decimal("1"), ["TST-A"],
                       lambda i: None, lambda i: False)
    assert snap2.view("TST-A").data_ok


def test_b6_backtest_snapshot_has_holding_days(tmp_path: Path) -> None:
    from tests.scenario.test_t009_backtest import config
    from trading_system.app.backtest import run_backtest
    seen: list[int] = []

    class Spy(type(config(tmp_path).strategy)):  # type: ignore[misc]
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            for v in snapshot.instruments:
                if v.position is not None:
                    seen.append(v.position.holding_days)
            return super().evaluate(snapshot, params)

    run_backtest(replace(config(tmp_path, "hd"), strategy=Spy()))
    assert seen and max(seen) > 0


# --- B-8 ----------------------------------------------------------------------------------------

@pytest.mark.parametrize("special", [
    [{"date": "2026-11-19", "open": "10:00", "close": "16:30"}],
    [{"date": date(2025, 11, 13), "open": "10:00", "close": "16:30"}],
])
def test_b8_special_session_date_must_be_date_of_year(special: list) -> None:
    with pytest.raises(MarketDataError):
        parse_calendar({"market": "KRX", "year": 2026, "source": "가상", "source_version": "t",
                        "special_sessions": special}, 2026)


# --- B-9 ----------------------------------------------------------------------------------------

def test_b9_masking_more_keys_and_formats(tmp_path: Path) -> None:
    log = JsonLogger(tmp_path, "app", Masker())
    log.write({"api_key": "k1", "X-MBX-APIKEY": "k2", "Authorization": "Bearer abc.def", "signature": "s1",
               "listenKey": "lk", "url": "https://h/x?token=SECRET123&a=1", "msg": "계좌 1234-5678-90, 123-456789-01, 12345678"},
              at=datetime(2026, 10, 1, tzinfo=UTC))
    text = next(tmp_path.glob("*.jsonl")).read_text(encoding="utf-8")
    for leaked in ("k1", "k2", "abc.def", "s1", "\"lk\"", "SECRET123", "1234-5678-90", "123-456789-01", "12345678"):
        assert leaked not in text, leaked


def test_b9_reconciler_query_error_text_is_masked(env: Env) -> None:
    from trading_system.broker.base import BrokerQueryError

    def boom(account: str):  # type: ignore[no-untyped-def]
        raise BrokerQueryError("GET https://api/x?token=SECRET999 acct 1234567890 실패")

    env.broker.query_positions = boom  # type: ignore[method-assign]
    rec(env).run(ACCOUNT, "PERIODIC")
    dump = json.dumps(env.repo.audit_events(), ensure_ascii=False) + json.dumps(
        env.repo.select("SELECT summary FROM reconciliation"), ensure_ascii=False)
    assert "SECRET999" not in dump and "1234567890" not in dump


# --- B-10 ----------------------------------------------------------------------------------------

def test_b10_retire_after_replace_stop_becomes_user_and_requires_no_holdings(env: Env) -> None:
    reg = StrategyRegistry(env.repo, env.clock, env.halts)
    spec = cross_spec("t-cross")
    reg.register(spec, dict(PARAMS), CAPS_ALL_SUPPORTED, scenario_limits(), Mode.PAPER)
    reg.transition("t-cross", 1, StrategyState.BACKTEST_PASSED, "A-901", evidence="bt.md")
    reg.transition("t-cross", 1, StrategyState.PAPER, "A-902")
    from trading_system.domain.models import Signal
    from trading_system.domain.enums import OrderType
    s = Signal("t-cross", 1, "e", ACCOUNT, "TST-A", "R", RequestType.ENTRY, Decimal("10"), OrderType.MARKET,
               Decimal("10000"), T0 - timedelta(minutes=1), T0 + timedelta(minutes=30))
    d = env.gate.decide(GateRequest.from_signal(s))
    env.executor.submit(d.approved)
    env.executor.process_events()
    reg.transition("t-cross", 1, StrategyState.STOPPED, "A-903", stop_reason=StopReason.REPLACE)
    with pytest.raises(ValueError, match="소유"):
        reg.transition("t-cross", 1, StrategyState.RETIRED, "A-904")   # 보유가 남아 있으면 폐기 불가
    ex = replace(s, evaluation_id="x", kind=RequestType.EXIT)
    d2 = env.gate.decide(GateRequest.from_signal(ex))
    env.executor.submit(d2.approved)
    env.executor.process_events()
    reg.transition("t-cross", 1, StrategyState.RETIRED, "A-904")
    [k9] = [h for h in env.repo.halt_rows(None) if h["halt_type"] == "K9"]
    assert k9["variant"] == "USER"


# --- B-11·B-12 -----------------------------------------------------------------------------------

def test_b11_apply_fill_that_is_rejected_does_not_resolve(env: Env) -> None:
    enter(env, qty="10")
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    red = enter(env, RequestType.REDUCE, qty="4", ev="red")
    order = env.repo.broker_order(env.repo.intent(red.approved.intent_id).broker_order_key)
    env.broker.fill(order.order_no)
    env.broker.drop_events()
    rec(env).run(ACCOUNT, "PERIODIC")
    it = item(env, ItemKind.MISSING_FILL)
    detail = {**json.loads(it["detail"]), "quantity": "999"}  # 브로커 조회가 주문 수량 초과 체결을 보고 → 이상 거부
    with env.repo.tx():
        env.repo._x("UPDATE recon_item SET detail=? WHERE item_id=?", (json.dumps(detail), it["item_id"]))
    with pytest.raises(ResolveError, match="반영되지 않"):
        res(env).resolve(it["item_id"], "APPLY_FILL", "u")
    assert env.repo.recon_item(it["item_id"])["state"] == "OPEN"
    kinds = [e["kind"] for e in env.repo.audit_events()]
    assert "RECONCILE_RESOLVE_FAILED" in kinds and "RECONCILE_RESOLVE" not in kinds


def test_b12_match_does_not_satisfy_k4_while_items_open(env: Env) -> None:
    no = env.broker.add_external_order(ACCOUNT, "TST-C", "BUY", Decimal("3"))
    rec(env).run(ACCOUNT, "PERIODIC")                       # 미확인 주문 1건
    env.broker._book.pop(no)                                 # 브로커 조회에서 사라짐(다음 날 등)
    r = rec(env).run(ACCOUNT, "MANUAL")
    assert r.result != "MATCH" or env.repo.open_recon_items(ACCOUNT)
    assert not env.halts.resume(HaltType.K4, ACCOUNT, "LOCAL", "u").ok


# --- B-13·B-14 -----------------------------------------------------------------------------------

def test_b13_migration_steps_are_statement_lists_and_trigger_bodies_work(tmp_path: Path) -> None:
    p = tmp_path / "m.db"
    Repository(p, Mode.BACKTEST).close()
    trigger = ("CREATE TRIGGER t_x BEFORE UPDATE ON halt BEGIN SELECT RAISE(ABORT, 'no; really'); END")
    assert migrate(p, tmp_path / "bk", {2: ["CREATE TABLE extra(a TEXT)", trigger]}, target=3) == [3]
    assert schema_version_of(p) == "3"
    from trading_system.storage.maintenance import MaintenanceError
    with pytest.raises(MaintenanceError, match="목록"):
        migrate(p, tmp_path / "bk", {3: "CREATE TABLE y(a)"}, target=4)  # 문자열 하나는 거부


def test_b6_real_migration_v1_to_v2_opens(tmp_path: Path) -> None:
    import sqlite3
    p = tmp_path / "v1.db"
    Repository(p, Mode.BACKTEST).close()
    c = sqlite3.connect(p)  # 기준선(1) DB 재현: 2에서 더한 열을 빼고 버전 1
    c.execute("ALTER TABLE gate_decision DROP COLUMN rule_id")
    c.execute("ALTER TABLE ownership DROP COLUMN released_at")
    c.execute("UPDATE meta SET value='1' WHERE key='schema_version'")
    c.commit()
    c.close()
    assert migrate(p, tmp_path / "bk") == [2]
    r = Repository(p, Mode.BACKTEST)
    assert {"rule_id"} <= {row[1] for row in r.connection.execute("PRAGMA table_info(gate_decision)")}
    r.close()


def test_b13_restore_test_on_non_db_file_reports_failure(tmp_path: Path) -> None:
    import sqlite3
    f = tmp_path / "x.db"
    sqlite3.connect(f).execute("CREATE TABLE z(a)").connection.commit()
    rep = restore_test(f, "BACKTEST")
    assert not rep.ok


def test_b14_log_file_named_by_kst_date(tmp_path: Path) -> None:
    log = JsonLogger(tmp_path, "app", Masker())
    log.write({"m": 1}, at=datetime(2026, 10, 1, 16, 30, tzinfo=UTC))  # KST 10-02 01:30
    assert (tmp_path / "app-20261002.jsonl").exists()
    assert time  # 표시용


def test_b6_history_holding_exit_reason_entries_and_no_lookahead(env: Env) -> None:
    from trading_system.domain.history import history_view

    def sig(kind: RequestType, rule: str, ev: str):  # type: ignore[no-untyped-def]
        now = env.clock.now()
        s = replace(make_signal(kind, quantity="10", evaluation=ev, rule=rule),
                    as_of=now - timedelta(minutes=1), valid_until=now + timedelta(minutes=30))
        d = env.gate.decide(GateRequest.from_signal(s))
        env.executor.submit(d.approved)
        env.executor.process_events()

    owner = "t001-fixed:1"
    sig(RequestType.ENTRY, "ENT-1", "e1")                                   # 목 10-01 진입
    entered_view = env.clock.now()
    env.clock.advance(timedelta(days=4))                                    # 월 10-05
    h = history_view(env.repo.strategy_history(ACCOUNT, owner, "TST-A"), env.clock.now(), None)
    assert (h.holding_days, h.entries_recent, h.last_exit_at) == (2, 1, None)  # 금·월 = 2거래일
    sig(RequestType.EXIT, "EXIT-1", "x1")
    raw = env.repo.strategy_history(ACCOUNT, owner, "TST-A")
    after = history_view(raw, env.clock.now() + timedelta(minutes=1), None)
    assert after.holding_days is None and after.last_exit_reason == "EXIT-1" and after.entries_recent == 1
    before = history_view(raw, entered_view + timedelta(days=1), None)     # 청산 전 시각에는 청산이 안 보임
    assert before.last_exit_at is None and before.last_exit_reason is None
    assert history_view(raw, env.clock.now() + timedelta(days=40), None, recent_days=20).entries_recent == 0
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY                           # 체결 0주로 끝난 진입은 세지 않음
    sig(RequestType.ENTRY, "ENT-1", "e2")
    env.broker.expire_open()
    env.executor.process_events()
    raw2 = env.repo.strategy_history(ACCOUNT, owner, "TST-A")
    assert history_view(raw2, env.clock.now() + timedelta(minutes=1), None).entries_recent == 1


# --- B-15 ----------------------------------------------------------------------------------------

def test_b15_perturbation_detects_scale_invariant_lookahead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """미래 봉끼리의 비율만 쓰는 누출은 균일 배율(×3)로는 신호가 같아 보인다. 봉마다 다른 배율·미래 봉 제거로 잡는다."""
    import trading_system.marketdata.store as store_mod
    from tests.scenario.test_t009_backtest import START, END, config, trading_days
    from tests.support.sample_strategies import CrossStrategy, make_signal as strat_signal
    from trading_system.app.backtest import perturbation_check
    from trading_system.domain.enums import OrderType

    class Ratio(CrossStrategy):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            out = []
            for v in snapshot.instruments:
                b = v.daily_bars
                if v.data_ok and len(b) >= 2 and v.position is None and b[-1].close > b[-2].close:
                    out.append(strat_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                            Decimal("1"), OrderType.MARKET, b[0].close, timedelta(minutes=30)))
            return out

    monkeypatch.setattr(store_mod, "daily_bar_available", lambda day, as_of, cal: True)  # 미래 봉 누출
    real_pp = store_mod.preprocess_daily

    def no_quality(*a, **k):  # type: ignore[no-untyped-def]
        # 품질 검사(급변 감지)가 균일 배율의 경계를 잡아 버리면 이 시험이 교란 방식 자체를 시험하지 못한다
        p = real_pp(*a, **k)
        return replace(p, report=replace(p.report, ok=True))

    monkeypatch.setattr(store_mod, "preprocess_daily", no_quality)
    cfg = replace(config(tmp_path), strategy=Ratio())
    days = [d for d in trading_days() if START <= d <= END]
    assert perturbation_check(cfg, days[5:40:5])
