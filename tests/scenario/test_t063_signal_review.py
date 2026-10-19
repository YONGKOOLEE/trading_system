"""CP-039 T-063 (A-115, A-127) — paper-review 지표 분리·신호 출처 봉 대조 (D-251·D-255).

V-323 가격 기록·지표 분리(실행·신호 대비·괴리·백테스트 가정 대비 4개 따로, 응답 원문 없음), V-325 대조·dust·한도 평가 불변
(testnet / binance-public 두 모드 같은 입력), D-255 (a) 저장 봉 ↔ 재수집본(A-110 CSV 형식) 대조. 네트워크 없음 — 가상 기록.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from tests.scenario.test_t062_signal_gate import EnterThenWatch, _two_fills
from tests.support.m2_engine_world import ACCOUNT, SYM, M2World
from trading_system.app.wiring import SignalInputs, StrategyInput
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import Mode
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT
from trading_system.domain.signal_source import PUBLIC_BAR_SOURCE
from trading_system.marketdata.signal_feed import SignalFacts
from trading_system.reporting.m2_signal_review import (
    collect_signal_review, compare_recollected_bars, load_recollected_csv, render_signal_review,
)
from trading_system.storage.repository import Repository

D = Decimal
THETA = D("0.01")


def _world(tmp: Path, ref: SignalFacts | None, *, behavior: FakeBehavior = FakeBehavior.ACCEPT_ONLY):  # type: ignore[no-untyped-def]
    strat = EnterThenWatch()
    w = M2World(tmp, price="101.00", behavior=behavior,
                strategies=[StrategyInput(strat, {"qty": "0.0600"}, (time(1, 0), time(2, 0)))],
                signal=SignalInputs(ref, THETA) if ref is not None else None)
    w.broker.ioc_limit = False
    _stamp(w, ref is not None)
    w.set_baseline()
    w.start_and_resume()
    return w


def _stamp(w: M2World, public: bool) -> None:
    """run 조립과 같이 새 DB에 신호 출처 메타 기록(binance-public만 — testnet은 쓰지 않음, V-326)."""
    if public:
        from trading_system.domain.signal_source import KIND_PUBLIC
        from trading_system.storage.market_meta import bind_signal_source
        bind_signal_source(w.repo.connection, KIND_PUBLIC, created=True)


# --- V-323 지표 4개 분리 ---------------------------------------------------------------------------------------

def test_v323_four_metric_groups_separate(tmp_path: Path) -> None:
    ref = SignalFacts((SYM,))
    w = _world(tmp_path, ref)
    _two_fills(w, ref)                       # 판정 P_s 100·P_x 101 → 체결 101 @P_s 100, 102 @P_s 101
    w.at(1, 30, 1)
    ref.update_reference(SYM, D("98.00"), w.clock.now())          # 다른 종목 없음 → 진입 시도 없음(보유 중)
    r = collect_signal_review(w.repo, ACCOUNT)
    ex = r.execution
    assert ex["fills"] == 2 and ex["partial_intents"] == 0 and ex["buy_limit_intents"] == 1
    assert ex["slippage_bps"] == [D("0"), D("99.0099")]           # (101−101)/101, (102−101)/101 ×1e4 (소수 4자리)
    sg = r.signal_relative
    assert sg["fills"] == 2 and sg["missing_reference"] == 0
    assert sg["diff_bps"] == [D("100.0000"), D("99.0099")]         # (101−100)/100, (102−101)/101
    dv = r.divergence
    assert dv["decisions"] == 1 and dv["g11s_rejections"] == 0 and dv["max"] == D("0.01")
    text = render_signal_review(r)
    for head in ("① 실행 지표", "② 신호 대비 지표", "③ 괴리 분포", "④ 백테스트 가정 대비"):
        assert head in text
    assert "실전 근거 아님" in text and "판정 제외" in text
    assert "{" not in text                                          # 응답 원문·JSON 덩어리 없음
    w.close()


def test_v323_exit_records_prices_without_rejecting(tmp_path: Path) -> None:
    from tests.support.m2_engine_world import FixedM2Strategy
    ref = SignalFacts((SYM,))
    w = M2World(tmp_path, signal=SignalInputs(ref, THETA),
                strategies=[StrategyInput(FixedM2Strategy(), {"qty": "0.0100"}, (time(1, 0), time(4, 0)))])
    _stamp(w, True)
    w.set_baseline()
    w.start_and_resume()
    w.at(1, 0, 1)
    ref.update_reference(SYM, D("10000.00"), w.clock.now())
    w.engine.tick()
    w.at(4, 0, 1)
    ref.update_reference(SYM, D("8000.00"), w.clock.now())          # 괴리 25% — 청산은 거부하지 않고 기록만
    w.engine.tick()
    rows = w.repo.select("SELECT request_type, result, limit_snapshot FROM gate_decision ORDER BY seq")
    exit_row = [r for r in rows if r["request_type"] == "EXIT"][0]
    snap = json.loads(exit_row["limit_snapshot"])
    assert exit_row["result"] == "APPROVED" and snap["signal_price"] == "8000.00" and snap["exec_price"] == "10000.00"
    r = collect_signal_review(w.repo, ACCOUNT)
    assert r.divergence["decisions"] == 2 and r.divergence["max"] == D("0.25")
    w.close()


def test_v323_testnet_mode_review_has_no_signal_section(tmp_path: Path) -> None:
    w = _world(tmp_path, None)
    _two_fills(w, None)
    r = collect_signal_review(w.repo, ACCOUNT)
    assert r.signal_source is None and render_signal_review(r) == ""   # 기존 집계 결과 불변
    w.close()


# --- V-325 대조·dust·한도 평가 불변 ---------------------------------------------------------------------------------

def _flow(tmp: Path, signal: bool) -> dict:
    from tests.support.m2_engine_world import FixedM2Strategy
    ref = SignalFacts((SYM,)) if signal else None
    w = M2World(tmp, strategies=[StrategyInput(FixedM2Strategy(), {"qty": "0.0100"}, (time(1, 0), time(4, 0)))],
                signal=SignalInputs(ref, THETA) if ref is not None else None)
    w.set_baseline()
    w.start_and_resume()
    for hh in (1, 2, 4, 5):
        w.at(hh, 0, 1)
        if ref is not None:
            ref.update_reference(SYM, D("9990.00"), w.clock.now())
        w.engine.tick()
    out = {
        "position": w.repo.position(ACCOUNT, SYM),
        "recon": [(r["trigger"], r["result"]) for r in w.repo.select("SELECT trigger, result FROM reconciliation "
                                                                      "ORDER BY created_at")],
        "daily": w.engine.account_state.daily_risk(ACCOUNT),
        "dust": w.engine.profile.dust.check(ACCOUNT, SYM),   # type: ignore[attr-defined]
        "fills": [(r["quantity"], r["price"]) for r in w.repo.select("SELECT quantity, price FROM fill ORDER BY at")],
        "halts": sorted(w.repo.active_halts(ACCOUNT)),
    }
    w.close()
    return out


def test_v325_reconcile_dust_limits_identical_across_modes(tmp_path: Path) -> None:
    a = _flow(tmp_path / "a", signal=False)
    b = _flow(tmp_path / "b", signal=True)
    assert a == b and len(a["fills"]) == 2


# --- D-255 (a) 저장 봉 ↔ 재수집본 대조 ----------------------------------------------------------------------------

CSV_HEAD = ("open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trades,taker_buy_base,"
            "taker_buy_quote\n")


def _csv_row(t: datetime, close: str) -> str:
    end = t + timedelta(minutes=1) - timedelta(milliseconds=1)
    iso = "%Y-%m-%dT%H:%M:%S.000Z"
    return f"{t:{iso}},100.00,101.00,99.00,{close},10.5,{end.strftime('%Y-%m-%dT%H:%M:%S')}.999Z,1000,5,4,400\n"


def test_d255_stored_public_bars_vs_recollected_csv(tmp_path: Path) -> None:
    t0 = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
    bars = [MinuteBar(SYM, 1, t0 + timedelta(minutes=i), D("100.00"), D("101.00"), D("99.00"), D("100.50"),
                      D("10.5"), NO_ADJUSTMENT) for i in range(4)]
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    try:
        with repo.tx():
            repo.insert_minute_bars(bars[:3], PUBLIC_BAR_SOURCE, t0 + timedelta(minutes=5))
            repo.insert_minute_bars(bars[3:], "binance-testnet", t0 + timedelta(minutes=5))   # 다른 출처 — 대조 제외
        csv = tmp_path / "BTC-1m.csv"
        csv.write_text(CSV_HEAD + _csv_row(t0, "100.50") + _csv_row(t0 + timedelta(minutes=1), "100.60")
                       + _csv_row(t0 + timedelta(minutes=4), "100.50"), encoding="utf-8")
        recol = load_recollected_csv(csv)
        cmp = compare_recollected_bars(repo, SYM, recol, start=t0, end=t0 + timedelta(minutes=10))
    finally:
        repo.close()
    assert cmp.matched == 1
    assert [d[0] for d in cmp.differing] == [t0 + timedelta(minutes=1)] and cmp.differing[0][1] == ("close",)
    assert cmp.only_stored == [t0 + timedelta(minutes=2)]
    assert cmp.only_recollected == [t0 + timedelta(minutes=4)]
    assert cmp.excluded_other_source == 1


def test_review_tool_m2_signal_section_only_for_public_db(tmp_path: Path) -> None:
    from trading_system.app.review_tool import m2_signal_section
    from trading_system.domain.signal_source import KIND_PUBLIC
    from trading_system.storage.market_meta import bind_signal_source
    from trading_system.storage.repository import OrderIdentity
    db = tmp_path / "trading.db"
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    repo.close()
    assert m2_signal_section(db, Mode.PAPER, "BN-PAPER-01") == ""        # testnet(메타 없음) — 기존 집계 불변
    repo = Repository(db, Mode.PAPER, order_identity=OrderIdentity.SYMBOL)
    try:
        bind_signal_source(repo.connection, KIND_PUBLIC, created=True)
        t0 = datetime(2026, 10, 10, 0, 0, tzinfo=UTC)
        with repo.tx():
            repo.insert_minute_bars([MinuteBar(SYM, 1, t0, D("100.00"), D("101.00"), D("99.00"), D("100.50"), D("10.5"),
                                               NO_ADJUSTMENT)], PUBLIC_BAR_SOURCE, t0 + timedelta(minutes=2))
    finally:
        repo.close()
    csv = tmp_path / "r.csv"
    csv.write_text(CSV_HEAD + _csv_row(t0, "100.50"), encoding="utf-8")
    text = m2_signal_section(db, Mode.PAPER, "BN-PAPER-01", csv, SYM)
    assert "① 실행 지표" in text and "일치 1, 값 다름 0" in text
