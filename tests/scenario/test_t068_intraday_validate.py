"""CP-040 T-068 + CP-041 확장(D-275~D-277) — M2 장중 백테스트·검증 실행기 확장. V-333·V-334·V-344~V-348,
DP-82(A-126 ②), DP-81 최종 확인 경로(A-124 ④, V-295), DP-88 (1), 재검토 지적(저빈도 경로 요율·집계 봉 연결).

네트워크 없음. 모든 봉은 가상이다(var/ 읽지 않음). 거래 규칙은 테스트넷 실측값 표(TESTNET_RULES)만 쓴다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t053_m2_minute_backtest import (
    DAY, ROOT, SYM, Script, cfg_of, day_bars, flat_bars, minute_csv, replace_bar, row, source_of, utc, write_csv,
)
from tests.support.m2_fakes import m2_limits
from trading_system.app import backtest_m2_minute as bm
from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2_cli import PAIRS, TESTNET_RULES, backtest_m2_main
from trading_system.app.backtest_m2_minute import (
    MinuteValidationInputs, perturbation_check_minute, render_minute_report, run_minute_backtest,
    run_minute_final_check, run_minute_validation, summary_of,
)
from trading_system.app.validation import ValidationError, Window
from trading_system.domain.daily_input import m2_daily_input
from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.fees import FeeModel
from trading_system.domain.market import CostRates
from trading_system.domain.minute_agg import check_agg_window
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.models import Signal
from trading_system.domain.strategy import (
    Bar, EvaluationSnapshot, InstrumentView, ParamSpec, PositionView, RuleSpec, StrategyLimits, StrategySpec,
    evaluation_id_of,
)
from trading_system.evaluation.runner import run_evaluation
from trading_system.strategy.base import ParamValues, make_signal

D = Decimal
ETH = "ETHUSDT"
BASE_CAPS = ("CAP-TRG-02", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05")
_IN = ("상태 입력",)


class Timed:
    """시험 전용: plan {"HH:MM": "ENTRY"|"EXIT"} — 그 판정 시각에 무보유면 qty 진입(손절가 stop), 내 소유 보유면 청산.
    판정 입력(시각·종목 입력)을 모두 기록한다(DP-82 두 번 판정이면 두 번 기록)."""

    def __init__(self, plan: dict[str, str], *, symbols: tuple[str, ...] = (SYM,), extra: tuple[str, ...] = (),
                 stop: str | None = None) -> None:
        self.plan, self.stop = plan, (D(stop) if stop else None)
        self.spec = StrategySpec(
            strategy_id="t-timed-1m", name="시험 전용 시각 대본", description="시험 전용", version=1, markets=("M2",),
            universe_method="U-A", universe=symbols, capabilities=BASE_CAPS + extra, data_requirements="시험",
            trigger="CAP-TRG-02 N분 격자",
            rules=(RuleSpec("EXIT-1", "EXIT", _IN, "CAP-TRG-02", "c", "a", "i", 1),
                   RuleSpec("ENTRY-1", "ENTRY", _IN, "CAP-TRG-02", "c", "a", "i", 1)),
            params=(ParamSpec("period_minutes", "int", "분", "격자", D("1"), D("720")),
                    ParamSpec("qty", "decimal", "기초", "수량", D("0.00001"), D("1000"))),
            signal_format="00 §2.5", validity_rule="다음 판정 전", validity_max_minutes=720,
            unfilled_policy="시스템 기본", sizing="S-4", holding="시험", limits=StrategyLimits())
        self.seen: list[tuple[datetime, InstrumentView]] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        act = self.plan.get(snapshot.as_of.astimezone(UTC).strftime("%H:%M"))
        out = []
        for v in snapshot.instruments:
            self.seen.append((snapshot.as_of, v))
            if not v.data_ok or v.last_price is None or v.has_pending_order:
                continue
            valid = timedelta(seconds=20)
            if act == "EXIT" and v.position is not None and v.position.owned_by_me:
                out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT, v.position.quantity,
                                       OrderType.MARKET, v.last_price, valid))
            elif act == "ENTRY" and v.position is None:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                       D(str(params["qty"])), OrderType.LIMIT, v.last_price, valid, stop_price=self.stop))
        return out


def fills(out) -> list[tuple[str, str, Decimal, Decimal]]:  # type: ignore[no-untyped-def]
    return [(f[2], f[3], D(f[4]), D(f[5])) for f in out.result.fills]


def bar(at: datetime, o: str, h: str, lo: str, c: str, v: str = "1000") -> Bar:
    return Bar(at, D(o), D(h), D(lo), D(c), D(v))


def stop_cfg(tmp: Path, bars: list[Bar], run_id: str = "s", declared: bool = True, days: int = 1):  # type: ignore[no-untyped-def]
    s = Timed({"01:00": "ENTRY"}, extra=("CAP-HLD-02",) if declared else (), stop="95")
    cfg = cfg_of(tmp, s, bars, params={"period_minutes": 60, "qty": D("1")}, run_id=run_id,
                 end=DAY + timedelta(days=days - 1))
    return replace(cfg, caps=None)   # CAP-HLD-02는 capabilities.md에 아직 없음(T-067 개정안 — 승인 뒤 교체)


# === V-333 장중 손절 체결 (D-262) ==============================================================================

def test_v333_stop_fill_next_minute_open_minus_slippage(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(3, 0), open=D("100"), high=D("100.5"), low=D("93.5"), close=D("94"))
    bars = replace_bar(bars, utc(3, 1), open=D("96"), high=D("96.5"), low=D("95.5"), close=D("96"))
    out = run_minute_backtest(stop_cfg(tmp_path, bars))
    assert fills(out) == [(SYM, "BUY", D("1"), D("100.10")), (SYM, "SELL", D("0.999"), D("95.90"))]
    st = out.stop
    assert (st["triggers"], st["fills"], st["unfilled"]) == (1, 1, 0)
    [ev] = st["excess"]
    assert ev["detected_bar"] == utc(3, 0).isoformat() and ev["stop"] == "95" and ev["fill"] == "95.90"
    assert D(ev["excess"]) == D("-0.90") and st["summary"]["gaps"] == 0


def test_v333_gap_below_stop_records_excess_loss(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(3, 0), open=D("100"), high=D("100.5"), low=D("93.5"), close=D("94"))
    bars = replace_bar(bars, utc(3, 1), open=D("90"), high=D("90.5"), low=D("89.5"), close=D("90"))
    out = run_minute_backtest(stop_cfg(tmp_path, bars))
    assert fills(out)[-1] == (SYM, "SELL", D("0.999"), D("89.91"))
    [ev] = out.stop["excess"]
    assert D(ev["excess"]) == D("5.09") and out.stop["summary"]["gaps"] == 1
    report = render_minute_report(stop_cfg(tmp_path, bars, run_id="s2"), out, reproducible=None, lookahead_bad=None)
    assert "손절 초과 손실" in report and "다음 분 시가" in report


def test_v333_missing_next_bar_unfilled_then_rejudged_and_missing_bar_not_detected(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(3, 0), open=D("100"), high=D("100.5"), low=D("93.5"), close=D("94"))
    bars = [b for b in bars if b.at != utc(3, 1)]                              # 체결 봉(감지 다음 분) 결측
    bars = replace_bar(bars, utc(3, 2), open=D("95"), high=D("95.5"), low=D("93.5"), close=D("94"))
    bars = replace_bar(bars, utc(3, 3), open=D("97"), high=D("97.5"), low=D("96.5"), close=D("97"))
    out = run_minute_backtest(stop_cfg(tmp_path, bars))
    assert fills(out) == [(SYM, "BUY", D("1"), D("100.10")), (SYM, "SELL", D("0.999"), D("96.90"))]
    st = out.stop
    assert (st["triggers"], st["fills"], st["unfilled"], st["not_judged"]) == (2, 1, 1, 1)
    assert out.result.rejects["MISSING_BAR"] == 1


def test_v333_future_bars_do_not_change_stop_detection(tmp_path: Path) -> None:
    """V-91: 감지 시각에 닫히지 않은 봉(체결 봉 다음부터)을 바꿔도 감지 시각·체결가 불변."""
    bars = replace_bar(day_bars(), utc(3, 0), open=D("100"), high=D("100.5"), low=D("93.5"), close=D("94"))
    a = run_minute_backtest(stop_cfg(tmp_path, bars, run_id="a"))
    later = [Bar(b.at, b.open * 3, b.high * 3, b.low * 3, b.close * 3, b.volume) if b.at >= utc(3, 2) else b  # type: ignore[operator]
             for b in bars]
    b = run_minute_backtest(stop_cfg(tmp_path, later, run_id="b"))
    assert a.stop["excess"] == b.stop["excess"] and fills(a) == fills(b)


def test_v333_stop_only_for_cap_hld_02_and_not_outside_order_window(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(3, 0), open=D("100"), high=D("100.5"), low=D("93.5"), close=D("94"))
    out = run_minute_backtest(stop_cfg(tmp_path, bars, declared=False))
    assert [f[1] for f in fills(out)] == ["BUY"] and out.stop["triggers"] == 0
    # 주문 창 밖(UTC 23:50~00:10)은 실운영 손절 감시도 보지 않는다(DP-89 (6)) — 같은 의미로 감지하지 않는다
    two = day_bars(DAY, days=2)
    two = replace_bar(two, utc(23, 55), open=D("100"), high=D("100.5"), low=D("93.5"), close=D("94"))
    out2 = run_minute_backtest(stop_cfg(tmp_path, two, run_id="w", days=2))
    assert [f[1] for f in fills(out2)] == ["BUY"] and out2.stop["triggers"] == 0


# === V-334 장중 L4·K2 (D-265) =================================================================================

def k2_bars() -> list[Bar]:
    two = day_bars(DAY, days=2)
    return replace_bar(two, utc(5, 30), open=D("100"), high=D("100.5"), low=D("50"), close=D("100"))


def test_v334_low_based_k2_blocks_entries_next_minute_allows_exit_and_resumes(tmp_path: Path) -> None:
    s = Timed({"01:00": "ENTRY", "08:00": "EXIT", "10:00": "ENTRY"})
    cfg = cfg_of(tmp_path, s, k2_bars(), params={"period_minutes": 60, "qty": D("49")}, end=DAY + timedelta(days=1))
    out = run_minute_backtest(cfg)
    assert [(f[3], f[0]) for f in out.result.fills] == [
        ("BUY", DAY), ("SELL", DAY), ("BUY", DAY + timedelta(days=1)), ("SELL", DAY + timedelta(days=1)),
        ("BUY", DAY + timedelta(days=1))]
    k2 = out.k2
    assert k2["days"] == 1 and k2["times"] == [utc(5, 30).isoformat()] and k2["hours"] == {"05": 1}
    assert k2["rejected_entries"] == {SYM: 1} and k2["close_days"] == 0 and k2["measured"] is True
    assert out.result.k2_resumes == 1
    report = render_minute_report(cfg, out, reproducible=None, lookahead_bad=None)
    assert "저가 기준" in report and "종가 기준" in report and "낙관" in report


def test_v334_close_based_reference_counted(tmp_path: Path) -> None:
    bars = replace_bar(k2_bars(), utc(5, 30), open=D("100"), high=D("100.5"), low=D("50"), close=D("50"))
    s = Timed({"01:00": "ENTRY"})
    out = run_minute_backtest(cfg_of(tmp_path, s, bars, params={"period_minutes": 60, "qty": D("49")}))
    assert out.k2["days"] == 1 and out.k2["close_days"] == 1


def test_v334_daily_runner_reports_no_intraday_l4(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_tm2_15_backtest_multi import cli_args, synthetic_csv
    data = synthetic_csv(tmp_path / "BTCUSDT-1d.csv", date(2025, 3, 1), 10)
    assert backtest_m2_main(cli_args(data, tmp_path / "o", "d1"), ROOT) == 0, capsys.readouterr().out
    report = (tmp_path / "o" / "d1" / "report.md").read_text(encoding="utf-8")
    assert "L4 장중 측정 불가" in report


# === V-344 여러 심볼 한 계좌 (D-275) ===========================================================================

def multi_cfg(tmp: Path, s: Timed, btc: list[Bar], eth: list[Bar], *, qty: str, cash: str = "10000",
              run_id: str = "m", eth_first: bool = False, **lim: object):  # type: ignore[no-untyped-def]
    cfg = cfg_of(tmp, s, btc, params={"period_minutes": 60, "qty": D(qty)}, run_id=run_id)
    srcs = {ETH: source_of(eth), SYM: source_of(btc)} if eth_first else {SYM: source_of(btc), ETH: source_of(eth)}
    return replace(cfg, sources=srcs, initial_cash=D(cash), account_limits=m2_limits(**lim))


def test_v344_combined_limit_second_signal_rejected_after_first_reservation(tmp_path: Path) -> None:
    s = Timed({"01:00": "ENTRY"}, symbols=(SYM, ETH), stop="90")
    out = run_minute_backtest(multi_cfg(tmp_path, s, day_bars(), day_bars(), qty="30",
                                        l2_max_symbol_ratio=D("0.40"), l3_max_total_ratio=D("0.50")))
    assert [(f[0], f[1]) for f in fills(out)] == [(SYM, "BUY")]          # 3000 + 3000 > L3 5000 → 뒤 신호(ETH) 거부
    assert sum(out.result.rejects.values()) == 1 and "MISSING_BAR" not in out.result.rejects


def test_v344_missing_fill_bar_only_that_symbol_and_order_independent_digest(tmp_path: Path) -> None:
    s = Timed({"01:00": "ENTRY", "02:00": "EXIT"}, symbols=(SYM, ETH), stop="90")
    eth = [b for b in day_bars() if b.at != utc(1, 0)]
    a = run_minute_backtest(multi_cfg(tmp_path, s, day_bars(), eth, qty="10", run_id="a"))
    assert [(f[0], f[1]) for f in fills(a)] == [(SYM, "BUY"), (SYM, "SELL")]
    assert a.result.rejects == {"MISSING_BAR": 1} and a.missing_fill == 1
    b = run_minute_backtest(multi_cfg(tmp_path, Timed({"01:00": "ENTRY", "02:00": "EXIT"}, symbols=(SYM, ETH), stop="90"),
                                      day_bars(), eth, qty="10", run_id="b", eth_first=True))
    assert a.result.digest == b.result.digest                            # 파일 인자 순서 무관 (V-344)
    cfg = multi_cfg(tmp_path, s, day_bars(), eth, qty="10", run_id="c")
    summ = summary_of(cfg, a, reproducible=None, lookahead_bad=None)
    assert summ["symbols"] == [SYM, ETH]
    assert summ["by_symbol"][SYM]["trades"] == 1 and summ["by_symbol"][ETH]["trades"] == 0
    assert summ["by_symbol"][ETH]["missing_bars"] == 1 and summ["by_symbol"][SYM]["missing_bars"] == 0
    report = render_minute_report(cfg, a, reproducible=None, lookahead_bad=None)
    assert "심볼별" in report and ETH in report


def test_v344_period_not_covered_by_all_symbols_refused(tmp_path: Path) -> None:
    s = Timed({"01:00": "ENTRY"}, symbols=(SYM, ETH))
    eth = [b for b in day_bars(DAY, days=2) if b.at >= utc(0, 0, day=DAY + timedelta(days=1))]   # 둘째 날부터 상장
    cfg = replace(multi_cfg(tmp_path, s, day_bars(DAY, days=2), eth, qty="1"), end=DAY + timedelta(days=1))
    with pytest.raises(BacktestError, match="모든 심볼"):
        run_minute_backtest(cfg)


# === V-345 확정 일봉 입력 (D-276) ==============================================================================

def daily_series(first: date, last: date, *, skip: frozenset[date] = frozenset(), close: dict | None = None) -> list[Bar]:
    out, d = [], first
    while d <= last:
        if d not in skip:
            c = (close or {}).get(d, D("100"))
            out.append(Bar(d, D("100"), max(D("100.5"), c), min(D("99.5"), c), c, D("1440000")))
        d += timedelta(days=1)
    return out


def daily_cfg(tmp: Path, s: Timed, daily: list[Bar], bars: list[Bar] | None = None, run_id: str = "d",
              days: int = 1):  # type: ignore[no-untyped-def]
    cfg = cfg_of(tmp, s, bars or day_bars(DAY, days=days), params={"period_minutes": 60, "qty": D("1")},
                 run_id=run_id, end=DAY + timedelta(days=days - 1))
    return replace(cfg, daily={SYM: tuple(daily)})


def test_v345_only_final_daily_bars_up_to_200_and_today_bar_ignored(tmp_path: Path) -> None:
    s = Timed({}, extra=("CAP-DAT-01",))
    daily = daily_series(DAY - timedelta(days=300), DAY + timedelta(days=1))
    run_minute_backtest(daily_cfg(tmp_path, s, daily))
    for at in (utc(1, 0, 2), utc(23, 0, 2)):
        [v] = [v for t, v in s.seen if t == at]
        assert v.daily_bars[-1].at == DAY - timedelta(days=1) and len(v.daily_bars) == 199 and v.data_ok
    # 당일(진행 중) 봉 교란·삭제 → 판정 입력 불변
    s2 = Timed({}, extra=("CAP-DAT-01",))
    changed = [replace(b, close=D("500"), high=D("500")) if b.at >= DAY else b for b in daily]   # type: ignore[operator]
    run_minute_backtest(daily_cfg(tmp_path, s2, changed, run_id="d2"))
    s3 = Timed({}, extra=("CAP-DAT-01",))
    run_minute_backtest(daily_cfg(tmp_path, s3, [b for b in daily if b.at < DAY], run_id="d3"))   # type: ignore[operator]
    assert [v.daily_bars for _, v in s.seen] == [v.daily_bars for _, v in s2.seen] == [v.daily_bars for _, v in s3.seen]


def test_v345_perturbation_check_covers_daily_bars(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    class Peek(Timed):
        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            out = []
            for v in snapshot.instruments:
                if v.daily_bars and v.daily_bars[-1].close > D("150") and v.last_price is not None:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY, D("1"),
                                           OrderType.LIMIT, v.last_price, timedelta(seconds=20)))
            return out
    daily = daily_series(DAY - timedelta(days=30), DAY + timedelta(days=1), close={DAY: D("200")})
    cfg = daily_cfg(tmp_path, Peek({}, extra=("CAP-DAT-01",)), daily)
    assert perturbation_check_minute(cfg, [DAY]) == []
    leaky = bm.m2_daily_input

    def peek_today(bars, as_of, finalize=None, **kw):  # type: ignore[no-untyped-def]
        return leaky(bars, as_of + timedelta(days=1), **kw)          # 진행 중 봉을 확정으로 — 룩어헤드
    monkeypatch.setattr(bm, "m2_daily_input", peek_today)
    assert perturbation_check_minute(cfg, [DAY]) != []


def test_v345_undeclared_strategy_gets_no_daily_and_consistency_report(tmp_path: Path) -> None:
    s = Timed({})
    daily = daily_series(DAY - timedelta(days=5), DAY + timedelta(days=1))
    bars = [b for b in day_bars(DAY, days=2) if b.at != utc(7, 0, day=DAY + timedelta(days=1))]   # 둘째 날 1분봉 불완전
    out = run_minute_backtest(daily_cfg(tmp_path, s, daily, bars, days=2))
    assert all(v.daily_bars == () for _, v in s.seen)                  # CAP-DAT-01 미선언 → 일봉 없음 (D-276 (6))
    chk = out.daily_check[SYM]
    assert (chk["compared"], chk["mismatch"], chk["minute_incomplete"]) == (1, 0, 1)
    bad = daily_series(DAY - timedelta(days=5), DAY + timedelta(days=1), close={DAY: D("101")})
    out2 = run_minute_backtest(daily_cfg(tmp_path, Timed({}), bad, bars, run_id="d2", days=2))
    assert out2.daily_check[SYM]["mismatch"] == 1 and D(out2.daily_check[SYM]["max_price_diff"]) == D("1")


def test_v345_cap_dat_01_without_daily_refused(tmp_path: Path) -> None:
    cfg = cfg_of(tmp_path, Timed({}, extra=("CAP-DAT-01",)), day_bars(), params={"period_minutes": 60, "qty": D("1")})
    with pytest.raises(BacktestError, match="CAP-DAT-01"):
        run_minute_backtest(replace(cfg, caps=None))


# === V-346 매분 L4 여러 심볼 (D-277) ===========================================================================

def test_v346_combined_low_triggers_k2_and_missing_symbol_minute_counted(tmp_path: Path) -> None:
    def dip(bars: list[Bar]) -> list[Bar]:
        return replace_bar(bars, utc(5, 30), open=D("100"), high=D("100.5"), low=D("70"), close=D("100"))
    s = Timed({"01:00": "ENTRY", "10:00": "ENTRY"}, symbols=(SYM, ETH))
    eth = [b for b in dip(day_bars()) if b.at != utc(6, 31)]
    out = run_minute_backtest(multi_cfg(tmp_path, s, dip(day_bars()), eth, qty="49", cash="100000"))
    assert out.k2["days"] == 1 and out.k2["times"] == [utc(5, 30).isoformat()]
    assert out.k2["eval_missing_minutes"] == 1
    one = run_minute_backtest(multi_cfg(tmp_path, Timed({"01:00": "ENTRY"}, symbols=(SYM, ETH)), dip(day_bars()),
                                        day_bars(), qty="49", cash="100000", run_id="m2"))
    assert one.k2["days"] == 0                                          # 한 심볼만 하락 — 합산 손실 < L4


# === V-347 단일 심볼·일봉 없음 호환 ===========================================================================

# 개정 전 코드(integ/2026-10-19b e76838b)로 같은 입력을 실행해 얻은 결과 지문
BEFORE = {"always": "6101cf37937217394433a4f07ca54958bfae367aa1d09bfc6080673d08623169",
          "v294": "5e9a5cdce892f8a345341ec62dd60fbe8b8efb22b6d8d56bb24949eed2f3286d",
          "twoday": "ebd00609df50600797f74181e62d3bcfb30417bba4cb25f5532458ee2b3d0847"}


def test_v347_single_symbol_fingerprints_unchanged(tmp_path: Path) -> None:
    a = run_minute_backtest(cfg_of(tmp_path, Script(always=True), day_bars(), run_id="a"))
    bars = replace_bar(day_bars(), utc(2, 0), open=D("110.00"), high=D("110.5"), close=D("110.00"))
    c = run_minute_backtest(cfg_of(tmp_path, Script(), bars, run_id="c"))
    two = [x for x in day_bars(DAY, days=2) if x.at != utc(6, 0)]
    d = run_minute_backtest(cfg_of(tmp_path, Script(always=True), two, params={"period_minutes": 60, "qty": D("1")},
                                   end=DAY + timedelta(days=1), run_id="d"))
    assert (a.result.digest, c.result.digest, d.result.digest) == (BEFORE["always"], BEFORE["v294"], BEFORE["twoday"])
    s = summary_of(cfg_of(tmp_path, Script(), bars, run_id="x"), c, reproducible=None, lookahead_bad=None)
    assert s["symbol"] == SYM and s["stop"]["triggers"] == 0 and s["daily_check"] is None


# === V-348 여러 심볼 검증 판정표 ===============================================================================

def test_v348_multi_symbol_validation_table(tmp_path: Path) -> None:
    btc = [b for b in day_bars(DAY, days=2) if b.at != utc(6, 0)]
    s = Timed({"01:00": "ENTRY", "02:00": "EXIT"}, symbols=(SYM, ETH), stop="90")
    tpl = multi_cfg(tmp_path, s, btc, day_bars(DAY, days=2), qty="1", cash="100000")
    ws = (Window(1, date(2024, 1, 1), date(2024, 12, 31), DAY, DAY),
          Window(2, date(2024, 1, 2), date(2025, 1, 1), DAY + timedelta(days=1), DAY + timedelta(days=1)))
    res = run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp_path / "val", data_start=date(2024, 6, 1),
                                                       windows=ws, perturbation_days=1, skip_variants=True))
    w1, w2 = res["windows"]
    assert w1["by_symbol"][SYM]["missing_bars"] == 1 and w1["by_symbol"][ETH]["missing_bars"] == 0
    assert res["pooled"]["trades"] == w1["trades"] + w2["trades"] == 4      # C-2 = 합산 거래 수
    assert res["symbols"] == [SYM, ETH] and SYM in res["verdict"]["C-7"] and ETH in res["verdict"]["C-7"]
    md = (tmp_path / "val" / "validation.md").read_text(encoding="utf-8")
    assert "심볼별" in md and "K2" in md and "손절" in md


# === DP-82 일봉 결측: 진입·증액만 막고 감액·청산 판정은 빈 날을 건너뛴 봉으로 ====================================

def day_bars_1d(first: date, last: date, skip: frozenset[date] = frozenset()) -> list[Bar]:
    return daily_series(first, last, skip=skip)


def test_dp82_daily_input_entry_tail_and_exit_full() -> None:
    gap = DAY - timedelta(days=3)
    bars = day_bars_1d(DAY - timedelta(days=10), DAY, frozenset({gap}))
    di = m2_daily_input(bars, utc(1, 0))
    assert [b.at for b in di.bars] == [DAY - timedelta(days=2), DAY - timedelta(days=1)] and di.ok
    assert len(di.exit_bars) == 9 and gap not in [b.at for b in di.exit_bars] and di.missing == (gap,)
    assert not di.complete
    no_y = m2_daily_input(day_bars_1d(DAY - timedelta(days=10), DAY - timedelta(days=2)), utc(1, 0))
    assert not no_y.ok and no_y.missing == (DAY - timedelta(days=1),) and len(no_y.exit_bars) == 9
    full = m2_daily_input(day_bars_1d(DAY - timedelta(days=300), DAY), utc(1, 0), max_bars=199)
    assert full.complete and full.ok and len(full.bars) == len(full.exit_bars) == 199 and full.missing == ()


class GapProbe(Timed):
    """보유 중 창 5봉 이상이면 청산, 무보유면 진입 — 판정 실행기의 DP-82 두 번 판정 확인용."""

    def __init__(self) -> None:
        super().__init__({})
        self.calls = 0

    def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
        self.calls += 1
        out = []
        for v in snapshot.instruments:
            if not v.data_ok or v.last_price is None or len(v.daily_bars) < 5:
                continue
            if v.position is not None:
                out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT, v.position.quantity,
                                       OrderType.MARKET, v.last_price, timedelta(seconds=20)))
            else:
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY, D("1"),
                                       OrderType.LIMIT, v.last_price, timedelta(seconds=20)))
        return out


def _snap(strategy: Timed, view: InstrumentView) -> EvaluationSnapshot:
    at = utc(1, 0)
    return EvaluationSnapshot(evaluation_id_of(strategy.spec.strategy_id, 1, "M2", at.isoformat()), "M2", "BN-BT-01",
                              at, D("10000"), (view,), next_evaluation_at=at + timedelta(hours=1))


def test_dp82_runner_exit_from_gap_skipped_bars_entry_dropped() -> None:
    gap = DAY - timedelta(days=3)
    di = m2_daily_input(day_bars_1d(DAY - timedelta(days=10), DAY, frozenset({gap})), utc(1, 0))
    held = PositionView(D("1"), D("100"), 0, True)
    st = GapProbe()
    v = InstrumentView(SYM, daily_bars=di.bars, last_price=D("100"), position=held, data_ok=True, daily_ok=di.ok,
                       daily_exit=di.exit_bars)
    r = run_evaluation(st, _snap(st, v), {}, {SYM: D("0.00001")})
    assert [s.kind for s in r.signals] == [RequestType.EXIT] and st.calls == 2   # 청산은 빈 날 건너뛴 9봉으로
    st2 = GapProbe()
    v2 = replace(v, position=None)
    r2 = run_evaluation(st2, _snap(st2, v2), {}, {SYM: D("0.00001")})
    assert r2.signals == [] and any("DP-82" in d.reason for d in r2.dropped)    # 진입은 두 번째 판정에서 나와도 버림
    st3 = GapProbe()
    no_y = m2_daily_input(day_bars_1d(DAY - timedelta(days=10), DAY - timedelta(days=2)), utc(1, 0))
    v3 = InstrumentView(SYM, daily_bars=no_y.bars, last_price=D("100"), position=held, data_ok=True,
                        daily_ok=no_y.ok, daily_exit=no_y.exit_bars)
    assert [s.kind for s in run_evaluation(st3, _snap(st3, v3), {}, {SYM: D("0.00001")}).signals] == [RequestType.EXIT]


def test_dp82_runner_complete_input_single_call_unchanged() -> None:
    st = GapProbe()
    full = m2_daily_input(day_bars_1d(DAY - timedelta(days=10), DAY), utc(1, 0))
    v = InstrumentView(SYM, daily_bars=full.bars, last_price=D("100"), data_ok=True, daily_ok=True, daily_exit=None)
    r = run_evaluation(st, _snap(st, v), {}, {SYM: D("0.00001")})
    assert [s.kind for s in r.signals] == [RequestType.ENTRY] and st.calls == 1


def test_dp82_minute_backtest_holding_through_daily_gap_exits(tmp_path: Path) -> None:
    """1분봉 실행기도 같은 함수: 보유 중 어제 일봉 결측 → 진입 막힘, 청산 판정은 빈 날 건너뛴 봉으로 계속."""
    s = GapProbe()
    s.spec = replace(s.spec, capabilities=BASE_CAPS + ("CAP-DAT-01",))
    daily = day_bars_1d(DAY - timedelta(days=20), DAY + timedelta(days=2), frozenset({DAY}))   # 둘째 날의 어제 결측
    cfg = daily_cfg(tmp_path, s, daily, days=2)
    out = run_minute_backtest(replace(cfg, params={"period_minutes": 720, "qty": D("1")}))
    sides = [(f[0], f[3]) for f in out.result.fills]
    assert sides == [(DAY, "BUY"), (DAY + timedelta(days=1), "SELL")]   # 12:00 판정 하루 1회 (720분 격자)


def test_dp82_live_exit_continues_on_missing_yesterday_with_telegram_warning(tmp_path: Path) -> None:
    """실운영 조립(build_engine M2): 보유 중 어제 1d 봉 결측 → 청산 신호가 브로커까지 가고(종전: data_ok False로 청산도 멈춤),
    텔레그램 경고(WARNING 감사 ALERT — 텔레그램이 보냄) 1건에 빈 날·보유 여부가 들어간다(DP-82 (나))."""
    from tests.support.m2_engine_world import DAY as WDAY
    from tests.support.m2_engine_world import SYM as WSYM
    from tests.support.m2_engine_world import M2World, m2_bars
    w = M2World(tmp_path)
    w.set_baseline()
    try:
        w.start_and_resume()
        w.tick_at(1, 0)                                                   # 완전한 일봉 → 진입(FixedM2Strategy)
        assert [r.side for r in w.sent()] == ["BUY"]
        gap = WDAY - timedelta(days=1)
        w.facts.set_daily_bars(WSYM, [b for b in m2_bars(WDAY - timedelta(days=60), WDAY + timedelta(days=10))
                                      if b.at != gap])
        w.tick_at(4, 0)
        assert [r.side for r in w.sent()] == ["BUY", "SELL"]              # 청산은 빈 날을 건너뛴 봉으로 계속
        msgs = [json.loads(e["summary"])["message"] for e in w.audits("ALERT")]
        warn = [m for m in msgs if "일봉 판정 입력 불완전" in m]
        assert len(warn) == 1 and "보유 있음(이 전략 소유" in warn[0] and gap.isoformat() in warn[0]
    finally:
        w.close()


# === DP-81 최종 확인 경로 (V-295) =============================================================================

def final_bars() -> list[Bar]:
    return flat_bars(datetime(2025, 12, 31, 23, 30, tzinfo=UTC), 30 + 1440 * 2)


def test_v295_final_check_once_per_version(tmp_path: Path) -> None:
    tpl = cfg_of(tmp_path, Script(always=True), final_bars(), params={"period_minutes": 60, "qty": D("1")})
    ledger = tmp_path / "final-usage.json"
    with pytest.raises(ValidationError, match="A-ID"):
        run_minute_final_check(tpl, tmp_path / "f0", final_from=date(2026, 1, 1), end=date(2026, 1, 2), ledger=ledger,
                               confirmed_by="")
    assert not ledger.exists()                                           # 입력 오류는 1회 사용을 소모하지 않음
    res = run_minute_final_check(tpl, tmp_path / "f1", final_from=date(2026, 1, 1), end=date(2026, 1, 2),
                                 ledger=ledger, confirmed_by="A-999")
    assert res["start"] == "2026-01-01" and res["end"] == "2026-01-02" and res["final-base"]["trades"] >= 1
    assert "final-slip2x" in res and (tmp_path / "f1" / "final-check.json").is_file()
    rec = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
    assert rec[0]["strategy"] == "t-script-1m" and rec[0]["kind"] == "final_check"
    with pytest.raises(ValidationError, match="한 번만"):
        run_minute_final_check(tpl, tmp_path / "f2", final_from=date(2026, 1, 1), end=date(2026, 1, 2),
                               ledger=ledger, confirmed_by="A-999")
    with pytest.raises(BacktestError, match="최종 확인"):                 # 일반 실행은 여전히 거부
        run_minute_backtest(replace(tpl, start=date(2026, 1, 1), end=date(2026, 1, 1), run_id="n"))


def test_v295_cli_final_check_second_run_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_t053_m2_minute_backtest import example_params
    data = minute_csv(tmp_path / "BTCUSDT-1m.csv", date(2026, 1, 1), 1)
    args = ["--interval", "1m", "--strategy", "tests.support.minute_strategies:SessionRangeBreakout",
            "--params", str(example_params(tmp_path / "p.toml", n=60)), "--data", str(data), "--lookback", "31",
            "--slippage", "0.001", "--out", str(tmp_path / "out"), "--final-check", "--confirmed-by", "A-999",
            "--end", "2026-01-01", "--ledger", str(tmp_path / "ledger.json")]
    assert backtest_m2_main([*args, "--run-id", "f1"], ROOT) == 0, capsys.readouterr().out
    assert backtest_m2_main([*args, "--run-id", "f2"], ROOT) == 2
    assert "한 번만" in capsys.readouterr().out


# === DP-88 (1) 공식 판정 경로 판정 주기 15·60 ======================================================================

def test_dp88_validate_requires_period_15_or_60(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_t053_m2_minute_backtest import example_params
    data = minute_csv(tmp_path / "BTCUSDT-1m.csv", date(2021, 6, 1), 1)
    base = ["--interval", "1m", "--strategy", "tests.support.minute_strategies:SessionRangeBreakout",
            "--data", str(data), "--lookback", "31", "--slippage", "0.001", "--out", str(tmp_path / "out")]
    assert backtest_m2_main([*base, "--params", str(example_params(tmp_path / "p.toml", n=5)), "--run-id", "v",
                             "--validate", "--skip-variants"], ROOT) == 2
    assert "15" in capsys.readouterr().out
    assert backtest_m2_main([*base, "--params", str(example_params(tmp_path / "p2.toml", n=5)), "--run-id", "e",
                             "--start", "2021-06-01", "--end", "2021-06-01"], ROOT) == 0     # 탐색 실행은 그대로


# === 재검토 지적 1: 저빈도 경로 요율 = 체결 요율(taker) ===========================================================

def test_low_frequency_path_uses_fill_rate_like_general_path(tmp_path: Path) -> None:
    from tests.scenario import test_t058_low_frequency as t058
    s = replace(t058.settings(), c2_min_trades=10 ** 6, lf1_min_window_trades=6, lf2_min_history_trades=6)
    bars = t058.lf_bars()
    tpl = replace(cfg_of(tmp_path, Script(enter_at="01:00", exit_at="02:00"), bars,
                         params={"period_minutes": 60, "qty": D("1")}),
                  account_limits=m2_limits(profile="research", approved_by="A-093"),
                  fees=FeeModel(D("0.002"), D("0.001"), "placeholder"),
                  cost_rates=CostRates(D("0.002"), D("0"), D("0.00000001")))   # maker > taker → 추정 0.002, 체결 0.001
    ws = tuple(Window(k + 1, date(2022, 1, 1), date(2024, 12, 31), t058.DAYS[2 * k], t058.DAYS[2 * k + 1])
               for k in range(3))
    o = run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp_path / "val", data_start=DAY - timedelta(1),
                                                     windows=ws, perturbation_days=1,
                                                     low_frequency=t058.lf_inputs(tmp_path, s, bars, lf8=None)))
    lf5 = o["low_frequency"]["items"]["LF-5"]["windows"]                  # LF-5는 하락 창만 나열
    general = {w["k"]: w["buy_hold_return"] for w in o["windows"]}
    assert lf5 and all(D(str(w["hold_return"])) == D(general[w["k"]]) for w in lf5)


# === 재검토 지적 2: 집계 봉(agg_bar_minutes) — 실운영과 같은 check_agg_window ===================================

def test_agg_bars_same_function_as_live(tmp_path: Path) -> None:
    s = Timed({"02:00": "ENTRY"}, extra=("CAP-DAT-02",))
    params = {"period_minutes": 60, "qty": D("1"), "agg_bar_minutes": 15, "agg_lookback_bars": 4}
    bars = day_bars()
    run_minute_backtest(replace(cfg_of(tmp_path, s, bars, params=params, lookback=5), caps=None))
    at = utc(2, 0, 2)
    [v] = [v for t, v in s.seen if t == at]
    want = check_agg_window([MinuteBar(SYM, 1, b.at, b.open, b.high, b.low, b.close, b.volume)  # type: ignore[arg-type]
                             for b in bars if b.at < utc(2, 0)], at, 15, 4)   # type: ignore[operator]
    assert v.minute_ok and v.agg_bars == want.bars and len(v.agg_bars) == 4 and v.agg_missing == ()
    s2 = Timed({"02:00": "ENTRY"}, extra=("CAP-DAT-02",))
    gap = [b for b in bars if b.at != utc(1, 50)]                          # 직전 집계 봉(01:45~02:00) 결측
    out = run_minute_backtest(replace(cfg_of(tmp_path, s2, gap, params=params, lookback=5, run_id="g"), caps=None))
    [v2] = [v for t, v in s2.seen if t == at]
    assert not v2.minute_ok and v2.agg_bars == () and out.result.fills == []


# === T-058 연결: LF-8 표본 1분봉 위치 (명령줄) ======================================================================

def test_lf8_sample_dir_wiring_builds_samples_or_reports_missing_files(tmp_path: Path) -> None:
    from tests.scenario.test_t058_low_frequency import FakeGit, _repo
    from trading_system.app.backtest_m2_cli import build_low_frequency_inputs, lf8_symbols
    root = _repo(tmp_path / "r")
    years = lf8_symbols(root / "docs" / "strategies" / "lf8" / "lf8-list.csv")
    assert years and all(isinstance(y, frozenset) for y in years.values())
    mdir = tmp_path / "lf8"
    mdir.mkdir()
    for sym in years:
        write_csv(mdir / f"{sym}-1m.csv", [row(utc(0, 0, day=date(2020, 1, 1))), row(utc(0, 1, day=date(2020, 1, 2)))])

    def build(**kw: object):  # type: ignore[no-untyped-def]
        return build_low_frequency_inputs(root, "t-lf", 1, {"period_minutes": 60}, out=tmp_path / "out",
                                          final_from=date(2026, 1, 1), data_start=date(2017, 8, 17), lookback=60,
                                          git=FakeGit(), **kw)
    low = build(lf8_minute_dir=mdir, aliases=(SYM,))
    assert low is not None and low.lf8 is not None and set(low.lf8.samples) == set(years)
    smp = next(iter(low.lf8.samples.values()))
    assert (smp.data_start, smp.data_end) == (date(2020, 1, 1), date(2020, 1, 2)) and low.lf8.aliases == (SYM,)
    (mdir / f"{sorted(years)[0]}-1m.csv").unlink()
    low2 = build(lf8_minute_dir=mdir, aliases=(SYM,))
    assert low2 is not None and low2.lf8 is None and "파일 없음" in low2.lf8_no_data_reason
    low3 = build()                                                       # 위치를 주지 않으면 지금처럼 판단 불가
    assert low3 is not None and low3.lf8 is None
