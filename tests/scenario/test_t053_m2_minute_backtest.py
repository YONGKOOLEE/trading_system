"""CP-037 T-053 (A-113 Q-5) — M2 1분봉 백테스트·검증 실행기 (D-221 체결·D-222 검증 창). V-294, V-295.

네트워크 없음. 데이터는 임시 폴더의 합성 1m CSV(A-110 열 형식)와 메모리 봉. 거래 규칙은 테스트넷 BTCUSDT 실측값(TESTNET_RULES).
예제 전략(tests/support/minute_strategies — 후보 전략 아님)으로 CAP-TRG-02·CAP-DAT-02 경로를 끝까지 통과시킨다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.m2_fakes import m2_limits
from tests.support.minute_strategies import SessionRangeBreakout
from trading_system.app import backtest_m2_minute as bm
from trading_system.app import main as app_main
from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2_cli import PAIRS, QUOTE_PRECISION, TESTNET_RULES
from trading_system.app.backtest_m2_minute import (
    MINUTE_COLUMNS, MinuteBacktestConfig, MinuteReadStats, MinuteValidationInputs, decision_points,
    iter_binance_minute_csv, m2_windows, perturbation_check_minute, run_minute_backtest, run_minute_validation,
)
from trading_system.app.validation import Window
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import OrderType, RequestType
from trading_system.domain.market import CostRates
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.minute_series import NO_ADJUSTMENT, PeriodGrid, check_window
from trading_system.domain.models import Signal
from trading_system.domain.strategy import Bar, EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec
from trading_system.strategy.base import ParamValues, make_signal

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
CAPS = load_capabilities(ROOT / "docs" / "capabilities.md")
SYM = "BTCUSDT"
DAY = date(2025, 3, 3)
HEADER = ",".join(MINUTE_COLUMNS) + "\n"
_IN = ("CAP-DAT-02", "상태 입력")


def utc(hh: int, mm: int, ss: int = 0, day: date = DAY) -> datetime:
    return datetime(day.year, day.month, day.day, hh, mm, ss, tzinfo=UTC)


def flat_bars(first: datetime, n: int, price: str = "100.00", vol: str = "1000") -> list[Bar]:
    p, v = D(price), D(vol)
    return [Bar(first + timedelta(minutes=i), p, p + D("0.5"), p - D("0.5"), p, v) for i in range(n)]


def day_bars(day: date = DAY, days: int = 1) -> list[Bar]:
    return flat_bars(utc(0, 0, day=day) - timedelta(minutes=30), 30 + 1440 * days)


def source_of(bars: list[Bar]):  # type: ignore[no-untyped-def]
    def src(start: datetime, stop: datetime):  # type: ignore[no-untyped-def]
        return [b for b in bars if start <= b.at < stop]  # type: ignore[operator]
    return src


class Script:
    """시험 전용: enter_at(UTC HH:MM) 판정에 무보유면 qty 진입, exit_at 판정에 보유면 청산. always면 무보유일 때마다 진입."""

    def __init__(self, enter_at: str = "01:00", exit_at: str = "02:00", always: bool = False) -> None:
        self.enter_at, self.exit_at, self.always = time.fromisoformat(enter_at), time.fromisoformat(exit_at), always
        self.spec = StrategySpec(
            strategy_id="t-script-1m", name="시험 전용 1분봉 대본", description="시험 전용", version=1, markets=("M2",),
            universe_method="U-A", universe=(SYM,), capabilities=("CAP-TRG-02", "CAP-DAT-02", "CAP-ORD-01",
                                                                  "CAP-ORD-02", "CAP-INS-05"),
            data_requirements="1분봉", trigger="CAP-TRG-02 N분 격자",
            rules=(RuleSpec("EXIT-1", "EXIT", _IN, "CAP-TRG-02", "c", "a", "i", 1),
                   RuleSpec("ENTRY-1", "ENTRY", _IN, "CAP-TRG-02", "c", "a", "i", 1)),
            params=(ParamSpec("period_minutes", "int", "분", "격자", D("1"), D("720")),
                    ParamSpec("qty", "decimal", "BTC", "수량", D("0.00001"), D("10"))),
            signal_format="00 §2.5", validity_rule="다음 판정 전", validity_max_minutes=720,
            unfilled_policy="시스템 기본", sizing="S-4", holding="시험", limits=StrategyLimits())
        self.snaps: list[EvaluationSnapshot] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        self.snaps.append(snapshot)
        hm = snapshot.as_of.astimezone(UTC).time().replace(second=0)
        out = []
        for v in snapshot.instruments:
            if not v.data_ok or v.last_price is None:
                continue
            valid = timedelta(seconds=20)
            if v.position is not None and v.position.owned_by_me and hm == self.exit_at:
                out.append(make_signal(self.spec, snapshot, SYM, "EXIT-1", RequestType.EXIT, v.position.quantity,
                                       OrderType.MARKET, v.last_price, valid))
            elif v.position is None and (self.always or hm == self.enter_at):
                out.append(make_signal(self.spec, snapshot, SYM, "ENTRY-1", RequestType.ENTRY, D(str(params["qty"])),
                                       OrderType.LIMIT, v.last_price, valid))
        return out


def cfg_of(tmp: Path, strategy: object, bars: list[Bar], *, params: dict | None = None, n: int = 5,
           lookback: int = 5, start: date = DAY, end: date = DAY, run_id: str = "r1",
           slippage: str = "0.001") -> MinuteBacktestConfig:
    return MinuteBacktestConfig(
        run_id=run_id, strategy=strategy, params=params or {"period_minutes": n, "qty": D("1")},  # type: ignore[arg-type]
        account_limits=m2_limits(), rules=dict(TESTNET_RULES), pairs=dict(PAIRS),
        cost_rates=CostRates(D("0.001"), D("0"), QUOTE_PRECISION), symbol=SYM, source=source_of(bars),
        start=start, end=end, initial_cash=D("100000"), out_dir=tmp, slippage=D(slippage), lookback_bars=lookback,
        caps=CAPS)


def replace_bar(bars: list[Bar], at: datetime, **kw: object) -> list[Bar]:
    return [replace(b, **kw) if b.at == at else b for b in bars]  # type: ignore[arg-type]


# --- 데이터 (스트리밍 로더) --------------------------------------------------------------------------------------

def row(at: datetime, o: str = "100", h: str = "101", lo: str = "99", c: str = "100", v: str = "1") -> str:
    end = at + timedelta(seconds=59, milliseconds=999)
    return (f"{at.strftime('%Y-%m-%dT%H:%M:%S')}.000Z,{o},{h},{lo},{c},{v},"
            f"{end.strftime('%Y-%m-%dT%H:%M:%S')}.999Z,1.0,3,0.5,50.0\n")


def write_csv(path: Path, rows: list[str]) -> Path:
    path.write_text(HEADER + "".join(rows), encoding="utf-8", newline="\n")
    return path


def test_loader_streams_range_and_never_parses_final_period_rows(tmp_path: Path) -> None:
    rows = [row(utc(23, 58, day=date(2025, 12, 31)) + timedelta(minutes=i)) for i in range(2)]
    rows.append("2026-01-01T00:00:00.000Z,garbage,x,y,z,w,2026-01-01T00:00:59.999Z,1,1,1,1\n")   # 해석하면 오류
    p = write_csv(tmp_path / "m.csv", [row(utc(0, 0, day=date(2025, 12, 30)))] + rows)
    st = MinuteReadStats()
    got = list(iter_binance_minute_csv(p, utc(0, 0, day=date(2025, 12, 31)), utc(0, 0, day=date(2027, 1, 1)),
                                       stats=st))
    assert [b.at for b in got] == [utc(23, 58, day=date(2025, 12, 31)), utc(23, 59, day=date(2025, 12, 31))]
    assert st.skipped_before == 1 and st.used == 2 and st.stopped_at == "2026-01-01T00:00:00.000Z"
    assert got[0].open == D("100") and isinstance(got[0].at, datetime) and got[0].at.tzinfo is not None


@pytest.mark.parametrize("bad,needle", [
    ("2025-03-03T00:01:30.000Z,1,2,0.5,1,1,2025-03-03T00:02:29.999Z,1,1,1,1\n", "분 경계"),
    ("2025-03-03T00:01:00.000Z,1,2,0.5,1,1,2025-03-03T00:01:30.000Z,1,1,1,1\n", "59.999"),
    ("2025-03-03T00:00:00.000Z,1,2,0.5,1,1,2025-03-03T00:00:59.999Z,1,1,1,1\n", "오름차순"),
    ("2025-03-03T00:01:00.000Z,1,2,0.5,3,1,2025-03-03T00:01:59.999Z,1,1,1,1\n", "품질"),
    ("2025-03-03T00:01:00.000Z,1,2,0.5,1,-1,2025-03-03T00:01:59.999Z,1,1,1,1\n", "품질"),
    ("2025-03-03T00:01:00.000Z,1,2,0.5,x,1,2025-03-03T00:01:59.999Z,1,1,1,1\n", "형식"),
    ("2025-03-03T00:01:00,1,2,0.5,1,1,2025-03-03T00:01:59.999,1,1,1,1\n", "형식"),
    ("2025-03-03T00:01:00.000Z,1,2,0.5,1,1,2025-03-03T00:01:59.999Z,1,1\n", "열 수"),
])
def test_loader_rejects_bad_rows_with_line_only(tmp_path: Path, bad: str, needle: str) -> None:
    p = write_csv(tmp_path / "m.csv", [row(utc(0, 0)), bad])
    with pytest.raises(ConfigError) as exc:
        list(iter_binance_minute_csv(p, utc(0, 0), utc(1, 0)))
    msg = str(exc.value)
    assert needle in msg and "m.csv:3" in msg


def test_loader_requires_a110_header(tmp_path: Path) -> None:
    p = tmp_path / "m.csv"
    p.write_text("open_time_utc,open,high,low,close,volume,close_time_utc\n" + row(utc(0, 0)), encoding="utf-8")
    with pytest.raises(ConfigError, match="A-110"):
        list(iter_binance_minute_csv(p, utc(0, 0), utc(1, 0)))


# --- 판정 시점 = 실운영 창 규칙 ----------------------------------------------------------------------------------

def test_decision_points_window_matches_live_check_window() -> None:
    import random
    rng = random.Random(7)
    bars = [b for b in day_bars() if rng.random() > 0.03]           # 결측 약 3%
    grid = PeriodGrid(5)
    for dp in decision_points(bars, grid, [DAY], 10):
        live = check_window([MinuteBar(SYM, 1, b.at, b.open, b.high, b.low, b.close, b.volume, NO_ADJUSTMENT)  # type: ignore[arg-type]
                             for b in bars if b.at < dp.at], dp.at, 10, grid.margin)   # type: ignore[operator]
        assert dp.minute_ok == (live.status.value == "OK")
        assert [b.at for b in dp.window] == [b.start for b in live.bars]
        assert dp.fill is None or dp.fill.at == dp.boundary
        assert dp.last is None or dp.last.at < dp.boundary            # type: ignore[operator]


def test_schedule_same_grid_as_live_inside_order_window(tmp_path: Path) -> None:
    s = Script(always=False)
    run_minute_backtest(cfg_of(tmp_path, s, day_bars(), n=15))
    times = [x.as_of for x in s.snaps]
    assert times == list(PeriodGrid(15).day_times(DAY))
    assert times[0] == utc(0, 15, 2) and times[-1] == utc(23, 45, 2)


# --- V-294 체결 ----------------------------------------------------------------------------------------------

def fills(out) -> list[tuple[str, Decimal, Decimal]]:  # type: ignore[no-untyped-def]
    return [(f[3], D(f[4]), D(f[5])) for f in out.result.fills]


def test_v294_buy_at_decision_minute_open_plus_slippage_sell_minus(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(2, 0), open=D("110.00"), high=D("110.5"), close=D("110.00"))
    out = run_minute_backtest(cfg_of(tmp_path, Script(), bars))
    # 100 × 1.001, 110 × 0.999 (tick 0.01). 매도 수량 = 받은 BTC(수수료 0.1% 차감) LOT_SIZE 내림 = 0.999
    assert fills(out) == [("BUY", D("1"), D("100.10")), ("SELL", D("0.999"), D("109.89"))]
    assert len(out.result.trades) == 1 and out.result.rejects == {}
    for f in out.result.fills:
        assert f[0] == DAY


def test_v294_buy_above_ioc_limit_not_filled(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(1, 0), open=D("101.00"), high=D("101.5"), close=D("101.00"))
    out = run_minute_backtest(cfg_of(tmp_path, Script(), bars))          # 지정가 = 직전 종가 100 × 1.005 = 100.50
    assert fills(out) == [] and out.result.trades == []


def test_v294_missing_fill_bar_unfilled_recorded(tmp_path: Path) -> None:
    bars = [b for b in day_bars() if b.at != utc(1, 0)]
    out = run_minute_backtest(cfg_of(tmp_path, Script(), bars))
    assert fills(out) == [] and out.result.rejects["MISSING_BAR"] == 1 and out.missing_fill == 1
    assert out.missing_bars == 1


def test_v294_volume_cap_10_percent_of_fill_bar(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(1, 0), volume=D("5"))
    out = run_minute_backtest(cfg_of(tmp_path, Script(), bars))
    assert fills(out)[0] == ("BUY", D("0.5"), D("100.10"))                  # 봉 거래량 5 × 10%


def test_v294_no_fill_outside_order_window_and_gap_in_window_invalid(tmp_path: Path) -> None:
    s = Script(always=True)
    bars = [b for b in day_bars() if b.at != utc(5, 2)]                  # 05:05 판정 창(05:00~05:04) 안 결측
    out = run_minute_backtest(cfg_of(tmp_path, s, bars))
    assert all(time(0, 10) <= x.as_of.time() < time(23, 50) for x in s.snaps)     # 판정은 주문 창 [00:10, 23:50) 안에서만 (A-124 ①)
    assert max(x.as_of for x in s.snaps) == utc(23, 45, 2)                # N=5 마지막 판정 23:45 (23:50 판정 없음)
    assert out.result.fills                                   # 체결은 판정 시각(창 안)에만 일어난다 (match at = 판정 시각)
    v = next(x for x in s.snaps if x.as_of == utc(5, 5, 2)).instruments[0]
    assert not v.minute_ok and v.minute_bars == ()                     # 무효 → 진입 신호는 실행기가 버림
    assert out.minute_invalid >= 1


def test_v294_reproducible_digest_and_final_guard(tmp_path: Path) -> None:
    bars = day_bars()
    a = run_minute_backtest(cfg_of(tmp_path, Script(always=True), bars, run_id="a"))
    b = run_minute_backtest(cfg_of(tmp_path, Script(always=True), bars, run_id="b"))
    assert a.result.digest == b.result.digest and a.result.fills
    with pytest.raises(BacktestError, match="최종 확인"):
        run_minute_backtest(cfg_of(tmp_path, Script(), bars, start=date(2025, 12, 31), end=date(2026, 1, 1), run_id="c"))


def test_v294_perturbation_passes_and_detects_leaky_input(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    import random
    rng = random.Random(3)
    bars = [Bar(b.at, b.open + D(rng.randint(0, 50)), b.high + D(60), b.low - D(1), b.close + D(rng.randint(0, 50)),
                b.volume) for b in day_bars()]
    bars = [replace(b, high=max(b.open, b.close, b.high), low=min(b.open, b.close, b.low)) for b in bars]
    cfg = cfg_of(tmp_path, Script(always=True), bars)
    assert perturbation_check_minute(cfg, [DAY]) == []
    original = bm.decision_points

    def leaky(*a, **kw):  # type: ignore[no-untyped-def]
        for dp in original(*a, **kw):
            yield replace(dp, last=dp.fill) if dp.fill is not None else dp   # 체결 봉(미래)을 최근가로 — 룩어헤드
    monkeypatch.setattr(bm, "decision_points", leaky)
    assert perturbation_check_minute(cfg, [DAY]) != []


# --- 예제 전략 (CAP-TRG-02 + CAP-DAT-02 끝까지) --------------------------------------------------------------------

def breakout_bars() -> list[Bar]:
    bars = day_bars()
    out = []
    for b in bars:
        if utc(3, 0) <= b.at < utc(3, 30):                      # 03:00~03:29 상승(시가 = 직전 종가) → 구간 돌파
            k = int((b.at - utc(3, 0)).total_seconds() // 60) + 1  # type: ignore[operator]
            p = D("100") + D(k)
            b = Bar(b.at, p - D("1"), p + D("0.2"), p - D("1.1"), p, b.volume)
        elif b.at >= utc(3, 30):                                 # 이후 계속 하락 → 구간 최저가 이탈 청산
            k = int((b.at - utc(3, 30)).total_seconds() // 60) + 1  # type: ignore[operator]
            p = max(D("90") - D(k), D("10"))
            b = Bar(b.at, p + D("1"), p + D("1.5"), p - D("0.5"), p, b.volume)
        out.append(b)
    return out


def test_example_session_range_strategy_end_to_end(tmp_path: Path) -> None:
    strat = SessionRangeBreakout()
    params = {"period_minutes": 5, "range_minutes": 30, "notional": D("1000"), "qty_step": D("0.00001"),
              "flat_time": "23:00"}
    out = run_minute_backtest(cfg_of(tmp_path, strat, breakout_bars(), params=params, lookback=31))
    sides = [f[0] for f in fills(out)]
    assert sides[:2] == ["BUY", "SELL"] and len(out.result.trades) >= 1
    assert out.result.signals and out.decisions == len(PeriodGrid(5).day_times(DAY))


def test_example_strategy_refused_for_m2_paper_registration() -> None:
    from trading_system.config.strategy_registry import validate_spec
    from trading_system.domain.enums import Mode
    spec = SessionRangeBreakout().spec
    assert validate_spec(spec, CAPS, m2_limits(), Mode.BACKTEST) == []
    assert validate_spec(spec, CAPS, m2_limits(), Mode.PAPER)        # "검증 중" — PAPER 불가 (후보 아님)


# --- V-295 검증 창 --------------------------------------------------------------------------------------------

def test_v295_m2_windows_2020_2025_six() -> None:
    ws = m2_windows()
    assert len(ws) == 6 and ws[0].train_start == date(2017, 1, 1) and ws[0].train_end == date(2019, 12, 31)
    assert [w.val_start.year for w in ws] == [2020, 2021, 2022, 2023, 2024, 2025]
    assert all(w.val_end == date(w.val_start.year, 12, 31) for w in ws)


def test_v295_validation_tiny_windows_report_flags(tmp_path: Path) -> None:
    bars = day_bars(DAY, days=2)
    bars = [b for b in bars if b.at != utc(6, 0)]                          # 창 1에 결측 1분
    tpl = cfg_of(tmp_path, Script(always=True), bars, params={"period_minutes": 60, "qty": D("1")})
    ws = (Window(1, date(2024, 1, 1), date(2024, 12, 31), DAY, DAY),
          Window(2, date(2024, 1, 2), date(2025, 1, 1), DAY + timedelta(days=1), DAY + timedelta(days=1)))
    res = run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp_path / "val",
                                                       data_start=date(2024, 6, 1), windows=ws, perturbation_days=1))
    w1, w2 = res["windows"]
    assert w1["train_shortened"] and w1["train_effective_start"] == "2024-06-01"
    assert w1["missing_bars"] == 1 and w2["missing_bars"] == 0
    assert w1["reproducible"] and w1["lookahead_bad"] == [] and w1["trades"] >= 1
    assert res["verdict"]["verdict"] == "판단 불가" and res["verdict"]["C-2"] == "판단 불가"
    assert "과대 추정" in res["verdict"]["C-7"]
    labels = [v["label"] for v in res["variants"]]
    assert labels == ["p-qtyplus", "p-qtyminus"]                           # period_minutes는 변형하지 않음
    md = (tmp_path / "val" / "validation.md").read_text(encoding="utf-8")
    assert "학습 단축" in md and "결측 분" in md and "C-7" in md
    assert json.loads((tmp_path / "val" / "validation.json").read_text(encoding="utf-8"))["pooled"]["missing_bars"] == 1


def test_v295_validation_refuses_window_into_final_period(tmp_path: Path) -> None:
    tpl = cfg_of(tmp_path, Script(), day_bars(), params={"period_minutes": 60, "qty": D("1")})
    ws = (Window(1, date(2022, 1, 1), date(2024, 12, 31), date(2025, 12, 31), date(2026, 1, 2)),)
    from trading_system.app.validation import ValidationError
    with pytest.raises(ValidationError):
        run_minute_validation(MinuteValidationInputs(template=tpl, out_dir=tmp_path / "v", data_start=DAY, windows=ws))


# --- 명령 ----------------------------------------------------------------------------------------------------

def minute_csv(path: Path, first: date, days: int) -> Path:
    rows = [row(utc(0, 0, day=first) + timedelta(minutes=i)) for i in range(1440 * days)]
    return write_csv(path, rows)


def example_params(path: Path, n: int = 5) -> Path:
    path.write_text('strategy = "t-session-range"\nversion = 1\n[params]\n'
                    f'period_minutes = {n}\nrange_minutes = 30\nnotional = "1000"\nqty_step = "0.00001"\n'
                    'flat_time = "23:00"\n', encoding="utf-8")
    return path


def test_cli_minute_backtest_writes_report_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = minute_csv(tmp_path / "BTCUSDT-1m.csv", DAY, 2)
    code = app_main.main(["backtest", "--market", "M2", "--interval", "1m", "--root", str(ROOT),
                          "--strategy", "tests.support.minute_strategies:SessionRangeBreakout",
                          "--params", str(example_params(tmp_path / "p.toml")), "--data", str(data),
                          "--start", "2025-03-03", "--end", "2025-03-04", "--run-id", "m1", "--lookback", "31",
                          "--slippage", "0.001", "--out", str(tmp_path / "out")])
    assert code == 0, capsys.readouterr().out
    s = json.loads((tmp_path / "out" / "m1" / "summary.json").read_text(encoding="utf-8"))
    assert s["interval"] == "1m" and s["decisions"] == 2 * len(PeriodGrid(5).day_times(DAY))
    assert s["reproducible"] is True and s["lookahead_bad"] == [] and s["missing_bars"] == 0
    report = (tmp_path / "out" / "m1" / "report.md").read_text(encoding="utf-8")
    assert "D-221" in report and "재현성(D-074): 통과" in report and "C-7" in report


def test_cli_minute_refuses_final_period_and_non_periodic(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = minute_csv(tmp_path / "BTCUSDT-1m.csv", date(2025, 12, 31), 1)
    base = ["--interval", "1m", "--params", str(example_params(tmp_path / "p.toml")), "--data", str(data),
            "--run-id", "x", "--lookback", "31", "--slippage", "0.001", "--out", str(tmp_path / "out")]
    from trading_system.app.backtest_m2_cli import backtest_m2_main
    assert backtest_m2_main(base + ["--strategy", "tests.support.minute_strategies:SessionRangeBreakout",
                                    "--start", "2025-12-31", "--end", "2026-01-01"], ROOT) == 2
    assert "최종 확인" in capsys.readouterr().out
    assert backtest_m2_main(["--interval", "1m", "--strategy", "trading_system.strategy.m2_verify:M2Verify",
                             "--params", "config/strategies/m2-verify-v1.toml", "--data", str(data),
                             "--start", "2025-12-31", "--end", "2025-12-31", "--run-id", "y", "--lookback", "5",
                             "--slippage", "0.001", "--out", str(tmp_path / "out")], ROOT) == 2
    assert "CAP-TRG-02" in capsys.readouterr().out


def test_cli_minute_validate_six_windows(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = minute_csv(tmp_path / "BTCUSDT-1m.csv", date(2021, 6, 1), 1)      # 대부분 결측 — 구조만 본다
    from trading_system.app.backtest_m2_cli import backtest_m2_main
    code = backtest_m2_main(["--interval", "1m", "--strategy", "tests.support.minute_strategies:SessionRangeBreakout",
                             "--params", str(example_params(tmp_path / "p.toml", n=720)), "--data", str(data),
                             "--run-id", "v1", "--lookback", "31", "--slippage", "0.001", "--validate",
                             "--skip-variants", "--out", str(tmp_path / "out")], ROOT)
    assert code == 0, capsys.readouterr().out
    v = json.loads((tmp_path / "out" / "v1" / "validation.json").read_text(encoding="utf-8"))
    assert [w["validation"][0] for w in v["windows"]] == [f"{y}-01-01" for y in range(2020, 2026)]
    assert v["windows"][0]["train_shortened"] and v["data_start"] == "2021-06-01"
    assert v["verdict"]["C-4"] == "미실행" and v["verdict"]["verdict"] == "판단 불가"
    assert all(w["missing_bars"] > 0 for w in v["windows"])


def test_cli_daily_path_unchanged_with_explicit_interval(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from tests.scenario.test_tm2_15_backtest_multi import cli_args, synthetic_csv
    from trading_system.app.backtest_m2_cli import backtest_m2_main
    data = synthetic_csv(tmp_path / "BTCUSDT-1d.csv", date(2025, 3, 1), 10)
    assert backtest_m2_main(cli_args(data, tmp_path / "o1", "d1"), ROOT) == 0
    assert backtest_m2_main(["--interval", "1d", *cli_args(data, tmp_path / "o2", "d1")], ROOT) == 0
    a = json.loads((tmp_path / "o1" / "d1" / "summary.json").read_text(encoding="utf-8"))
    b = json.loads((tmp_path / "o2" / "d1" / "summary.json").read_text(encoding="utf-8"))
    assert a["digest"] == b["digest"]


def test_daily_equity_uses_that_days_last_closed_bar(tmp_path: Path) -> None:
    """A-124 재검토 지적 2: 날짜별 평가액 = 그날 마지막 닫힌 봉(23:59) 종가 — 다음 날 첫 판정 가격이나 마지막 판정 가격이 아님."""
    day2 = DAY + timedelta(days=1)
    bars = (flat_bars(utc(0, 0) - timedelta(minutes=30), 30 + 1430)                 # 1일차 ~23:49 100
            + flat_bars(utc(23, 50), 10, price="120.00")                            # 1일차 23:50~23:59 120 (창 밖)
            + flat_bars(utc(0, 0, day=day2), 1425, price="150.00")                  # 2일차 ~23:44 150
            + flat_bars(utc(23, 45, day=day2), 15, price="160.00"))                 # 2일차 23:45~23:59 160 (마지막 판정 뒤)
    out = run_minute_backtest(cfg_of(tmp_path, Script(enter_at="01:00", exit_at="23:59"), bars, end=day2))
    assert fills(out)[0][0] == "BUY"
    (d1, e1), (d2, e2) = out.result.equity
    assert (d1, d2) == (DAY, day2)
    held = (e2 - e1) / D("40")                                    # 2일차 거래 없음 → 차이 = 보유 × (160 − 120)
    assert D("0.99") < held <= D("1")                             # 옛 계산: 두 날 모두 150 → 차이 0
