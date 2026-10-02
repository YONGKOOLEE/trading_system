"""T-009 백테스트 엔진 (05-plan T-009, 04 §2·D-071~D-077, V-90~V-92, CP-016 A-5).

모든 가격·달력·전략은 가상이다 (형식·동작 시험용, 실제 시장 데이터 아님).
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.gate_fakes import scenario_limits
from tests.support.invariants import check_invariants
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, PARAMS, CrossStrategy
from trading_system.app.backtest import (
    BacktestConfig, BacktestError, perturbation_check, render_report, run_backtest, run_sensitivity, segment_days,
)
from trading_system.broker.fake.fill_model import BacktestBroker, BarFacts
from trading_system.config.strategy_params import validate_params
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode, OrderType
from trading_system.domain.market import CostRates, GateSettings, InstrumentInfo, InstrumentKind
from trading_system.domain.models import OrderRequest
from trading_system.marketdata.calendar import KrxCalendar, parse_calendar
from trading_system.marketdata.quality import RawDaily
from trading_system.storage.repository import Repository

CAL = KrxCalendar({2026: parse_calendar({"market": "KRX", "year": 2026, "source": "가상", "source_version": "t",
                                          "holidays": [date(2026, 2, 16)]}, 2026)})
START, END = date(2026, 1, 5), date(2026, 3, 31)


def trading_days() -> list[date]:
    out, d = [], START - timedelta(days=10)
    while d <= END:
        if CAL.is_trading_day(d):
            out.append(d)
        d += timedelta(days=1)
    return out


def path(pattern: list[int]) -> list[RawDaily]:
    """가상 종가 패턴을 거래일에 반복 배치. 시가 = 전일 종가, 고가·저가 = ±1%."""
    rows, prev = [], Decimal(pattern[0])
    for i, d in enumerate(trading_days()):
        c = Decimal(pattern[i % len(pattern)])
        o = prev
        rows.append(RawDaily(d, o, max(o, c) * Decimal("1.01"), min(o, c) * Decimal("0.99"), c, Decimal("100000")))
        prev = c
    return rows


DATA = {"TST-A": path([8500, 8800, 9100, 9400, 9600, 9300, 8900, 8400, 7900, 7600, 8000, 8300]),
        "TST-B": path([10000, 10100, 10200, 10100, 10000])}
INFOS = {i: InstrumentInfo(i, InstrumentKind.STOCK, Decimal("1"), Decimal("1")) for i in DATA}
SETTINGS = GateSettings(60, Decimal("0.30"), CostRates(Decimal("0.00015"), Decimal("0.0018")))


def config(tmp_path: Path, run_id: str = "r1", **over: object) -> BacktestConfig:
    strat = CrossStrategy()
    cfg = BacktestConfig(
        run_id=run_id, strategy=strat, params=validate_params(strat.spec, PARAMS), caps=CAPS_ALL_SUPPORTED,
        account_limits=scenario_limits(l7_max_order_amount=Decimal("10000000")), gate_settings=SETTINGS,
        calendar=CAL, ticks=None, slippage_tick=Decimal("10"), data=DATA, infos=INFOS, start=START, end=END,
        initial_cash=Decimal("10000000"), out_dir=tmp_path / "var" / "backtest", data_version="virtual-v1",
        code_commit="test", seed=7, final_usage_ledger=tmp_path / "var" / "backtest" / "final-usage.jsonl")
    return replace(cfg, **over)  # type: ignore[arg-type]


# --- 종단 실행 ------------------------------------------------------------------------------------

def test_backtest_runs_through_real_gate_and_records_everything(tmp_path: Path) -> None:
    r = run_backtest(config(tmp_path))
    assert r.trades, "가상 패턴에서 진입·청산 왕복이 있어야 함"
    assert int(r.metrics["trades"]) == len(r.trades)
    assert r.db_path == tmp_path / "var" / "backtest" / "r1" / "trading.db"
    repo = Repository(r.db_path, Mode.BACKTEST)
    assert check_invariants(repo.connection, "BACKTEST") == []
    decisions = repo.connection.execute("SELECT COUNT(*) FROM gate_decision").fetchone()[0]
    assert decisions == len(r.signals) > 0  # 모든 신호가 실운영 게이트를 거침 (D-071)
    repo.close()
    assert Decimal(r.metrics["costs"]) > 0
    assert r.equity[0][0] == START or r.equity[0][0] > START


def test_v92_reproducible_same_input_same_digest(tmp_path: Path) -> None:
    a = run_backtest(config(tmp_path, "a"))
    b = run_backtest(config(tmp_path, "b"))
    assert a.digest == b.digest and a.metrics == b.metrics


def test_a5_same_run_id_refused(tmp_path: Path) -> None:
    run_backtest(config(tmp_path, "same"))
    with pytest.raises(BacktestError, match="같은 실행 ID"):
        run_backtest(config(tmp_path, "same"))


def test_costs_reduce_equity_exactly(tmp_path: Path) -> None:
    free = run_backtest(config(tmp_path, "free", gate_settings=GateSettings(60, Decimal("0.30"),
                                                                           CostRates(Decimal("0"), Decimal("0")))))
    paid = run_backtest(config(tmp_path, "paid"))
    assert free.fills == paid.fills  # 같은 체결
    assert Decimal(free.metrics["final_equity"]) - Decimal(paid.metrics["final_equity"]) == paid.costs


def test_gate_limits_apply_in_backtest(tmp_path: Path) -> None:
    tiny = scenario_limits(l1_max_loss_ratio=Decimal("0.001"), l2_max_symbol_ratio=Decimal("0.001"),
                           l7_max_order_amount=Decimal("10000000"))
    r = run_backtest(config(tmp_path, "tight", account_limits=tiny))
    assert r.rejects.get("G7", 0) > 0 and r.trades == []


def test_quality_failure_blocks_instrument(tmp_path: Path) -> None:
    broken = dict(DATA)
    rows = list(DATA["TST-A"])
    rows[20] = replace(rows[20], close=rows[20].close * 5)  # 이상치
    broken["TST-A"] = rows
    bad_day = rows[20].day
    r = run_backtest(config(tmp_path, "q", data=broken))
    assert "TST-A" in r.quality_failures
    # 이상치 봉이 확정된 다음 날부터는 그 종목으로 판정하지 않는다 (그 전에는 정상 판정 — 미래 품질이 과거를 막지 않음)
    assert all(not (f[2] == "TST-A" and f[0] > bad_day) for f in r.fills)
    assert any(f[2] == "TST-A" and f[0] <= bad_day for f in r.fills)


# --- 룩어헤드 (V-90·V-91) ------------------------------------------------------------------------

def test_v91_perturbation_check_passes_for_honest_pipeline(tmp_path: Path) -> None:
    days = segment_days([d for d in trading_days() if START <= d <= END], "ALL")
    assert perturbation_check(config(tmp_path), days[5:40:5]) == []


def test_v91_perturbation_check_detects_lookahead(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import trading_system.marketdata.store as store_mod
    monkeypatch.setattr(store_mod, "daily_bar_available", lambda day, as_of, cal: True)  # 미래 봉 누출
    days = [d for d in trading_days() if START <= d <= END]
    assert perturbation_check(config(tmp_path), days[5:40:5])


# --- 구간·최종 평가 (D-075) ------------------------------------------------------------------------

def test_segments_60_20_20() -> None:
    days = list(range(100))
    assert [len(segment_days(days, s)) for s in ("TRAIN", "VALIDATION", "FINAL")] == [60, 20, 20]  # type: ignore[arg-type]


def test_final_segment_used_once_per_strategy_version(tmp_path: Path) -> None:
    run_backtest(config(tmp_path, "f1", segment="FINAL"))
    with pytest.raises(BacktestError, match="한 번만"):
        run_backtest(config(tmp_path, "f2", segment="FINAL"))
    with pytest.raises(BacktestError, match="사용 장부"):
        run_backtest(config(tmp_path, "f3", segment="FINAL", final_usage_ledger=None))


def test_missing_calendar_and_unsupported_capability_refused(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="달력 없음"):
        run_backtest(config(tmp_path, "c1", end=date(2027, 1, 10)))
    caps = {k: {"M1": "미지원", "M2": "미지원", "M3": "미지원"} for k in CAPS_ALL_SUPPORTED}
    with pytest.raises(BacktestError, match="등록 거부"):
        run_backtest(config(tmp_path, "c2", caps=caps))


# --- 보고서 (§2.8) ---------------------------------------------------------------------------------

def test_report_has_required_fields_and_verdict(tmp_path: Path) -> None:
    cfg = config(tmp_path, "rep")
    r = run_backtest(cfg)
    again = run_backtest(replace(cfg, run_id="rep2"))
    sens = run_sensitivity(replace(cfg, run_id="sens"), (0, 1, 2))
    text = render_report(cfg, r, settings_hash=scenario_limits().version_hash(),
                         reproducible=r.digest == again.digest, lookahead_bad=[], sensitivity=sens)
    for needle in ("데이터: 버전 `virtual-v1`", "설정 해시", "코드 커밋", "시드: 7", "체결 가정", "비용", "구간",
                   "max_drawdown", "민감도", "한계", "재현성(D-074): 통과", "룩어헤드 교란 검사(D-073): 통과",
                   "백테스트 통과 조건 충족", "결과 지문"):
        assert needle in text, needle
    assert Decimal(sens[0]["total_return"]) >= Decimal(sens[2]["total_return"])  # 슬리피지가 클수록 불리


# --- 체결 모델 (D-072) ------------------------------------------------------------------------------

def _broker(**kw: object) -> BacktestBroker:
    return BacktestBroker(lambda i, p: Decimal("10"), **kw)  # type: ignore[arg-type]


AT = datetime(2026, 1, 5, 9, 0, tzinfo=KST).astimezone(UTC)


def _order(b: BacktestBroker, side: str = "BUY", qty: str = "10", ot: OrderType = OrderType.MARKET,
           price: str = "10000") -> str:
    return b.place_order(OrderRequest("NEW", "A", "TST-A", side, Decimal(qty), ot, Decimal(price))).order_no or ""


def test_market_order_fills_next_bar_open_with_slippage() -> None:
    b = _broker()
    _order(b, "BUY")
    _order(b, "SELL")
    b.match_bar("TST-A", BarFacts(Decimal("10000"), Decimal("10200"), Decimal("9900"), Decimal("1000")), AT)
    prices = sorted((e.price for e in b.poll_events()))
    assert prices == [Decimal("9990"), Decimal("10010")]  # 매도 −1틱, 매수 +1틱


@pytest.mark.parametrize(("side", "limit", "filled"), [
    ("BUY", "9900", False),   # 저가 = 지정가 (접촉) → 미체결
    ("BUY", "9910", True),    # 관통
    ("SELL", "10200", False),
    ("SELL", "10190", True),
])
def test_limit_order_needs_penetration(side: str, limit: str, filled: bool) -> None:
    b = _broker()
    _order(b, side, ot=OrderType.LIMIT, price=limit)
    b.match_bar("TST-A", BarFacts(Decimal("10000"), Decimal("10200"), Decimal("9900"), Decimal("1000")), AT)
    ev = b.poll_events()
    assert bool(ev) is filled and (not ev or ev[0].price == Decimal(limit))


def test_volume_cap_partial_then_expire_end_of_day() -> None:
    b = _broker()
    _order(b, "BUY", qty="150")
    b.match_bar("TST-A", BarFacts(Decimal("10000"), Decimal("10100"), Decimal("9900"), Decimal("1000")), AT)
    [fill] = b.poll_events()
    assert fill.quantity == Decimal("100")  # 거래량 1,000 × 10%
    b.end_of_day()
    assert [e.kind for e in b.poll_events()] == ["EXPIRED"]


def test_no_fill_at_limit_up_buy_or_halt() -> None:
    b = _broker()
    _order(b, "BUY")
    b.match_bar("TST-A", BarFacts(Decimal("13000"), Decimal("13000"), Decimal("13000"), Decimal("1000"),
                                  upper_limit=Decimal("13000")), AT)
    b.match_bar("TST-A", BarFacts(Decimal("10000"), Decimal("10000"), Decimal("10000"), Decimal("1000"), halted=True), AT)
    assert b.poll_events() == []


def test_backtest_broker_rejects_modify() -> None:
    b = _broker()
    r = b.modify_order(OrderRequest("MODIFY", "A", "TST-A", "BUY", Decimal("1"), OrderType.LIMIT, Decimal("1"),
                                    target_order_no="0000001"))
    assert r.outcome.value == "REJECTED"
    assert time(9, 5)  # 표시용
