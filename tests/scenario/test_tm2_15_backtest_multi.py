"""T-M2-15 백테스트 M2 하루 여러 판정 시각 (A-111 Q-1 (가), M1 DP-42와 같은 일봉 근사) + 공개 일봉 적재 + M2 백테스트 명령.

검증:
- 판정 시각 1개의 결과는 보강 전과 바이트 단위로 같다(결과 지문·보고서 고정값 — 보강 전 코드 abbdda1에서 잰 값).
- 하루 여러 판정(DP-42): 첫 판정 주문 = 그날 시가 + 불리 슬리피지(체결 시각 = 첫 판정 시각), 이후 판정 주문 = 그날
  마지막 판정 뒤 종가 + 불리 슬리피지(체결 시각 = 마지막 판정 시각), 모든 판정의 판정 시세 = 그날 시가, 그날 판정은
  모두 전날까지 확정 봉만 본다(V-90·V-142). IOC 지정가(시가 × 1.005 내림)는 종가 체결가가 넘으면 체결 0(EXPIRED).
- m2-verify 하루 왕복(01:00 매수 → 04:00 매도 → dust → 다음 날 재진입), dust 집계, 등록 검사(capabilities.md M2 열).
- 판정 시각 오름차순·중복 금지, 최종 확인 구간 보호(CP-030 D-163) 그대로, 교란 검사(V-91)가 모든 판정 시각을 본다.
- 바이낸스 공개 1d klines CSV 적재(형식 검사), `python -m trading_system.app backtest --market M2` 명령.
가격·잔고·수수료율·슬리피지는 시험용 가상값이다. 데이터는 합성 파일(실제 수집 파일을 쓰지 않음). 네트워크 없음.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_tm2_12_backtest import CHECK_DAYS, config as cross_config, spec as cross_spec
from tests.support.m2_fakes import m2_limits
from trading_system.app import backtest_m2 as bm
from trading_system.app import main as app_main
from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2 import (
    SpotBacktestConfig, dust_summary, perturbation_check_m2, render_report_m2, run_backtest_m2,
)
from trading_system.app.backtest_m2_cli import backtest_m2_main, load_binance_daily_csv
from trading_system.broker.fake.spot import virtual_symbol_rules
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.market import CostRates
from trading_system.domain.models import Signal
from trading_system.domain.strategy import Bar, EvaluationSnapshot
from trading_system.storage.repository import Repository
from trading_system.strategy.base import ParamValues, make_signal
from trading_system.strategy.m2_verify import M2Verify

ROOT = Path(__file__).resolve().parents[2]
D = Decimal
SYM = "BTCUSDT"
PAIRS = {SYM: ("BTC", "USDT")}
RULES = virtual_symbol_rules(SYM)          # 테스트넷 §12 형태의 가상 규칙 (tick 0.01, step 0.00001, minNotional 5)
RATES = CostRates(commission_rate=D("0.001"), sell_tax_rate=D("0"), cost_quantum=D("0.00000001"))
SLIP = D("0.001")                          # 시험용 가상 슬리피지
TIMES = (time(1, 0), time(4, 0))           # UTC — m2-verify 제안 기본값 (A-111 Q-2)
PARAMS = {"notional": D("10"), "qty_step": D("0.00001"), "split_time": "02:30"}
CAPS = load_capabilities(ROOT / "docs" / "capabilities.md")

# 보강 전(abbdda1) 판정 시각 1개 실행의 결과 지문·보고서 해시 (test_tm2_12 config 그대로)
DIGEST_0015 = "41bf906b56a1c62df70cefbbd039d7be96230f95b6372e5fa55d6e7752fd7dc4"
DIGEST_0100 = "0023181dfd521f82e2742d766df2a2c0c3cfd76f47950e195de3cccc78364db0"
REPORT_0015 = "b3cff7534393bbdec9cff07ff043d331b6f40d15bd964b795890c1cf216e1aeb"


def btc_bars(rows: list[tuple[str, str]], start: date = date(2025, 3, 1)) -> list[Bar]:
    """가상 UTC 일봉 (시가, 종가). 고가·저가 = 둘의 ±1%, 거래량 1000 BTC."""
    out = []
    for i, (o, c) in enumerate(rows):
        op, cl = D(o), D(c)
        out.append(Bar(start + timedelta(days=i), op, max(op, cl) * D("1.01"), min(op, cl) * D("0.99"), cl, D("1000")))
    return out


ROWS = [("60000", "61000"), ("61000", "59500"), ("59500", "60000")]


def cfg(tmp_path: Path, strategy: object, data: list[Bar], times: tuple[time, ...] = TIMES,
        **over: object) -> SpotBacktestConfig:
    base = dict(run_id="mt", strategy=strategy, params=dict(PARAMS), account_limits=m2_limits(), rules={SYM: RULES},
                pairs=PAIRS, cost_rates=RATES, data={SYM: data}, start=data[0].at, end=data[-1].at,
                initial_cash=D("10000"), out_dir=tmp_path, slippage=SLIP, eval_times=times)
    base.update(over)
    return SpotBacktestConfig(**base)  # type: ignore[arg-type]


def fill_rows(db: Path) -> list[tuple[str, Decimal, Decimal]]:
    repo = Repository(db, Mode.BACKTEST)
    try:
        return [(r["at"], D(r["quantity"]), D(r["price"])) for r in repo.select("SELECT at, quantity, price FROM fill")]
    finally:
        repo.close()


# --- 판정 시각 1개 = 보강 전과 같음 ---------------------------------------------------------------------------

def test_single_time_digest_and_report_unchanged(tmp_path: Path) -> None:
    c = cross_config(tmp_path, run_id="a")
    r = run_backtest_m2(c)
    assert r.digest == DIGEST_0015
    report = render_report_m2(c, r, reproducible=True, lookahead_bad=[])
    assert hashlib.sha256(report.replace(r.digest, "").encode()).hexdigest() == REPORT_0015
    assert run_backtest_m2(cross_config(tmp_path, run_id="b", eval_times=(time(0, 15),))).digest == DIGEST_0015
    assert run_backtest_m2(cross_config(tmp_path, run_id="c", eval_time=time(1, 0))).digest == DIGEST_0100
    assert run_backtest_m2(cross_config(tmp_path, run_id="d", eval_times=(time(1, 0),))).digest == DIGEST_0100


# --- 하루 여러 판정 (DP-42 일봉 근사) ---------------------------------------------------------------------------

def test_m2_verify_round_trip_each_day_open_buy_close_sell(tmp_path: Path) -> None:
    c = cfg(tmp_path, M2Verify(), btc_bars(ROWS))
    r = run_backtest_m2(c)
    assert not r.rejects, r.rejects
    assert set(r.signals_by_eval) == {f"2025-03-0{d}T{t}Z" for d in (1, 2, 3) for t in ("01:00", "04:00")}
    buys = [(f[0], D(f[4]), D(f[5])) for f in r.fills if f[3] == "BUY"]
    sells = [(f[0], D(f[4]), D(f[5])) for f in r.fills if f[3] == "SELL"]
    # 매수 = 그날 시가 × 1.001 tick 올림 (01:00 = 첫 판정), 수량 = 10 ÷ 시가 내림(0.00001)
    assert buys == [(date(2025, 3, 1), D("0.00016"), D("60060.00")), (date(2025, 3, 2), D("0.00016"), D("61061.00")),
                    (date(2025, 3, 3), D("0.00016"), D("59559.50"))]
    # 매도 = 그날 종가 × 0.999 tick 내림 (04:00 = 이후 판정), 수량 = 보유(순 수령 + 전날 dust)의 LOT 내림 (D-208)
    assert sells == [(date(2025, 3, 1), D("0.00015"), D("60939.00")), (date(2025, 3, 2), D("0.00016"), D("59440.50")),
                     (date(2025, 3, 3), D("0.00016"), D("59940.00"))]
    # 체결 시각 = 판정 시각 (첫 판정 01:00, 이후 판정 = 마지막 판정 04:00) — 주문 생성보다 앞서지 않음
    ats = sorted(at for at, _, _ in fill_rows(r.db_path))
    assert [datetime.fromisoformat(a).astimezone(UTC).time() for a in ats] == [time(1, 0), time(4, 0)] * 3
    assert len(r.trades) == 3 and r.metrics["trades"] == "3"
    events, residual = dust_summary(r.fills, c)
    assert events == 3 and residual == D("0.00000952")    # 0.00016968 − 0.00016 = 0.00000968 → … → 0.00000952


def test_later_decision_buy_fills_at_close_or_expires(tmp_path: Path) -> None:
    class LateBuy:
        """두 번째 판정(03:00 이후)에만 무보유면 고정 수량 진입. 가상 시험 전략."""

        def __init__(self) -> None:
            self.spec = replace(cross_spec(), strategy_id="late-buy", universe=(SYM,))

        def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
            out = []
            for v in snapshot.instruments:
                if snapshot.as_of.time() >= time(3, 0) and v.position is None and not v.has_pending_order \
                        and v.last_price is not None:
                    out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                           D("0.00016"), OrderType.LIMIT, v.last_price, timedelta(minutes=30)))
            return out

    # 종가 59000 × 1.001 = 59059.00 ≤ IOC 지정가 60000 × 1.005 = 60300.00 → 체결 (체결 시각 04:00)
    r = run_backtest_m2(cfg(tmp_path, LateBuy(), btc_bars([("60000", "59000")]), run_id="fill", params={}))
    assert [(f[3], D(f[5])) for f in r.fills] == [("BUY", D("59059.00"))]
    assert [datetime.fromisoformat(a).astimezone(UTC).time() for a, _, _ in fill_rows(r.db_path)] == [time(4, 0)]
    # 종가 61000 × 1.001 = 61061.00 > 60300.00 → 체결 0 (IOC 만료), 게이트 거부 아님
    r2 = run_backtest_m2(cfg(tmp_path, LateBuy(), btc_bars([("60000", "61000")]), run_id="exp", params={}))
    assert r2.fills == [] and not r2.rejects and len(r2.signals) == 1


def test_every_decision_sees_only_prior_days_and_open_as_price(tmp_path: Path) -> None:
    class Seen:
        def __init__(self) -> None:
            self.spec = replace(cross_spec(), strategy_id="seen", universe=(SYM,))
            self.seen: list[tuple[datetime, date | None, Decimal | None]] = []

        def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
            for v in snapshot.instruments:
                self.seen.append((snapshot.as_of, v.daily_bars[-1].at if v.daily_bars else None,  # type: ignore[arg-type]
                                  v.last_price))
            return []

    data = btc_bars(ROWS)
    s = Seen()
    run_backtest_m2(cfg(tmp_path, s, data, times=(time(1, 0), time(4, 0), time(23, 0)), params={}))
    opens = {b.at: b.open for b in data}
    assert len(s.seen) == 9
    for as_of, last, price in s.seen:
        assert last is None or last == as_of.date() - timedelta(days=1)    # 그날 봉(진행 중)은 23:00에도 없음
        assert price == opens[as_of.date()]                                 # 판정 시세 = 그날 시가 (종가는 미래)


def test_times_must_be_ascending_and_unique(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="오름차순"):
        run_backtest_m2(cfg(tmp_path, M2Verify(), btc_bars(ROWS), times=(time(4, 0), time(1, 0))))
    with pytest.raises(BacktestError, match="오름차순"):
        run_backtest_m2(cfg(tmp_path, M2Verify(), btc_bars(ROWS), times=(time(1, 0), time(1, 0))))


def test_final_period_guard_applies_to_multi_time(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="최종 확인"):
        run_backtest_m2(cfg(tmp_path, M2Verify(), btc_bars(ROWS), final_from=date(2025, 3, 3)))


def test_v91_perturbation_checks_every_decision_time(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    days = [date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3)]
    assert perturbation_check_m2(cfg(tmp_path, M2Verify(), btc_bars(ROWS)), days) == []
    multi = cross_config(tmp_path, eval_times=(time(0, 15), time(4, 0)))
    assert perturbation_check_m2(multi, CHECK_DAYS) == []
    monkeypatch.setattr(bm, "final_bars", lambda data, as_of: tuple(b for b in data if b.at <= as_of.date()))
    assert "2025-03-02" in perturbation_check_m2(multi, CHECK_DAYS)


def test_multi_time_report_states_daily_bar_approximation(tmp_path: Path) -> None:
    c = cfg(tmp_path, M2Verify(), btc_bars(ROWS), data_source="합성 시험 데이터")
    r = run_backtest_m2(c)
    report = render_report_m2(c, r, reproducible=True, lookahead_bad=[])
    assert "01:00, 04:00 UTC" in report and "DP-42" in report and "합성 시험 데이터" in report


# --- 등록 검사 (capabilities.md M2 열) --------------------------------------------------------------------------

def test_registration_with_capabilities_passes_for_m2_verify_and_keeps_result(tmp_path: Path) -> None:
    plain = run_backtest_m2(cfg(tmp_path, M2Verify(), btc_bars(ROWS), run_id="plain"))
    reg = run_backtest_m2(cfg(tmp_path, M2Verify(), btc_bars(ROWS), run_id="reg", caps=CAPS))
    assert reg.digest == plain.digest
    repo = Repository(reg.db_path, Mode.BACKTEST)
    try:
        rows = repo.select("SELECT result, target_mode FROM strategy_registration")
        assert [(x["result"], x["target_mode"]) for x in rows] == [("PASSED", "BACKTEST")]
    finally:
        repo.close()


def test_registration_refused_for_capability_not_in_m2_column(tmp_path: Path) -> None:
    with pytest.raises(BacktestError, match="전략 등록 거부"):
        run_backtest_m2(cross_config(tmp_path, caps=CAPS))      # CAP-INS-01(국내 상장주식) — M2 "해당 없음"


# --- 바이낸스 공개 1d klines CSV 적재 -----------------------------------------------------------------------------

HEADER = ("open_time_utc,open,high,low,close,volume,close_time_utc,quote_volume,trades,taker_buy_base,"
          "taker_buy_quote\n")


def kline_row(day: date, o: str, h: str, low: str, c: str, v: str = "1000.00000000") -> str:
    return (f"{day.isoformat()}T00:00:00.000Z,{o},{h},{low},{c},{v},{day.isoformat()}T23:59:59.999Z,"
            f"1.0,10,1.0,1.0\n")


def synthetic_csv(path: Path, start: date, n: int) -> Path:
    lines = [HEADER]
    price = D("60000")
    for i in range(n):
        o = price
        c = price + (D("500") if i % 2 == 0 else D("-300"))
        lines.append(kline_row(start + timedelta(days=i), f"{o:.8f}", f"{max(o, c) * D('1.01'):.8f}",
                               f"{min(o, c) * D('0.99'):.8f}", f"{c:.8f}"))
        price = c
    path.write_text("".join(lines), encoding="utf-8", newline="\n")
    return path


def test_loader_reads_public_kline_format(tmp_path: Path) -> None:
    bars = load_binance_daily_csv(synthetic_csv(tmp_path / "BTCUSDT-1d.csv", date(2025, 3, 1), 3))
    assert [b.at for b in bars] == [date(2025, 3, 1), date(2025, 3, 2), date(2025, 3, 3)]
    assert bars[0].open == D("60000.00000000") and bars[0].close == D("60500.00000000")
    assert bars[0].volume == D("1000.00000000") and isinstance(bars[0].at, date) and not isinstance(bars[0].at, datetime)


def test_loader_incomplete_bar_skipped_only_when_asked(tmp_path: Path) -> None:
    """거래 중단으로 일찍 끝난 1d 봉(봉 끝 < 그날 23:59:59.999, 예: 바이낸스 2018-02-08 00:28 중단)은 기본 거부.
    incomplete 목록을 주면 그 봉을 빼고 날짜를 기록한다(04 §2.1 결측은 채우지 않음 → 그날 판정 데이터 없음, 주문 없음)."""
    p = tmp_path / "x.csv"
    p.write_text(HEADER + kline_row(date(2025, 3, 1), "1", "2", "0.5", "1")
                 + "2025-03-02T00:00:00.000Z,1,2,0.5,1,1,2025-03-02T00:28:14.788Z,1,1,1,1\n"
                 + kline_row(date(2025, 3, 3), "1", "2", "0.5", "1"), encoding="utf-8")
    with pytest.raises(ConfigError, match="봉 끝"):
        load_binance_daily_csv(p)
    skipped: list[date] = []
    bars = load_binance_daily_csv(p, incomplete=skipped)
    assert [b.at for b in bars] == [date(2025, 3, 1), date(2025, 3, 3)] and skipped == [date(2025, 3, 2)]


@pytest.mark.parametrize("bad", [
    "2025-03-02T01:00:00.000Z,1,2,0.5,1,1,2025-03-03T00:59:59.999Z,1,1,1,1\n",     # 봉 시작이 UTC 00:00 아님
    "2025-03-02T00:00:00.000Z,1,2,0.5,1,1,2025-03-03T00:28:14.788Z,1,1,1,1\n",     # 봉 끝이 다음 날 (1d 봉 아님)
    "2025-03-02T00:00:00.000Z,1,2,0.5,1,1,2025-03-02T11:59:59.999Z,1,1,1,1\n",     # 1d 봉이 아님 (close_time)
    "2025-03-02T00:00:00,1,2,0.5,1,1,2025-03-02T23:59:59.999,1,1,1,1\n",           # 시간대 없음 (naive)
    "2025-03-02T00:00:00.000Z,1,2,0.5,x,1,2025-03-02T23:59:59.999Z,1,1,1,1\n",     # 숫자 아님
    "2025-03-01T00:00:00.000Z,1,2,0.5,1,1,2025-03-01T23:59:59.999Z,1,1,1,1\n",     # 중복·역순 날짜
])
def test_loader_rejects_bad_rows(tmp_path: Path, bad: str) -> None:
    p = tmp_path / "x.csv"
    p.write_text(HEADER + kline_row(date(2025, 3, 1), "1", "2", "0.5", "1") + bad, encoding="utf-8")
    with pytest.raises(ConfigError):
        load_binance_daily_csv(p)


def test_loader_rejects_missing_columns(tmp_path: Path) -> None:
    p = tmp_path / "x.csv"
    p.write_text("open_time_utc,open,high,low,close\n2025-03-01T00:00:00.000Z,1,1,1,1\n", encoding="utf-8")
    with pytest.raises(ConfigError, match="열"):
        load_binance_daily_csv(p)


# --- M2 백테스트 명령 -----------------------------------------------------------------------------------------

def cli_args(data: Path, out: Path, run_id: str, end: str = "2025-03-08", eval_time: str = "01:00,04:00") -> list[str]:
    return ["--strategy", "trading_system.strategy.m2_verify:M2Verify",
            "--params", "config/strategies/m2-verify-v1.toml", "--data", str(data), "--start", "2025-03-01",
            "--end", end, "--run-id", run_id, "--eval-time", eval_time, "--slippage", "0.001", "--out", str(out)]


def test_cli_runs_m2_verify_and_writes_report_and_summary(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = synthetic_csv(tmp_path / "BTCUSDT-1d.csv", date(2025, 3, 1), 10)   # 03-10까지 — 실행 끝 뒤 행은 넘기지 않음
    code = app_main.main(["backtest", "--market", "M2", "--root", str(ROOT),
                          *cli_args(data, tmp_path / "out", "cli-a")])
    assert code == 0, capsys.readouterr().out
    run_dir = tmp_path / "out" / "cli-a"
    report = (run_dir / "report.md").read_text(encoding="utf-8")
    assert "01:00, 04:00 UTC" in report and "재현성(D-074): 통과" in report and "V-91): 통과" in report
    summary = json.loads((run_dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["round_trips"] == 8 and summary["fills"] == {"BUY": 8, "SELL": 8}
    assert summary["dust_events"] == 8 and summary["rejects"] == {} and summary["registration"] == "PASSED"
    assert summary["fee_rate"] == "0.001" and summary["slippage"] == "0.001"
    assert summary["data_rows_used"] == 8 and summary["data_rows_after_end_dropped"] == 2
    assert summary["incomplete_bars_skipped"] == []


def test_cli_refuses_final_period_and_bad_deploy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = synthetic_csv(tmp_path / "BTCUSDT-1d.csv", date(2025, 12, 28), 6)
    assert backtest_m2_main(cli_args(data, tmp_path / "out", "f", end="2026-01-02") + [], ROOT) == 2
    assert "최종 확인" in capsys.readouterr().out
    assert backtest_m2_main(cli_args(data, tmp_path / "out", "g", end="2025-12-31", eval_time="01:00"), ROOT) == 2
    assert "split_time" in capsys.readouterr().out
    assert not (tmp_path / "out").exists() or not any((tmp_path / "out").iterdir())
