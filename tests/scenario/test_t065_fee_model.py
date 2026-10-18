"""수수료 모델 적용 (CP-040 D-267, T-065). V-336 백테스트·보고서 부분 + PAPER isMaker 감사 기록.

- 백테스트 체결: 시스템 주문은 IOC 지정가 매수·시장가 매도뿐 → 모든 체결에 taker_rate(받는 자산에서 차감, D-101).
- 비용 추정(게이트): max(maker, taker) = cost_rates.commission_rate. 둘이 어긋난 설정은 실행 거부(조용한 불일치 방지).
- 요율이 같으면(maker = taker = 기존 commission_rate) 기존 결과(지문·지표·체결)와 같다.
- 보고서: 두 요율·출처, rate_source = "placeholder"면 "요율 자리값" 경고. fees를 주지 않은 기존 호출은 보고서 그대로.
- PAPER: 체결 통보의 isMaker를 FILL 감사 요약에 is_maker로 남긴다(수수료·수수료 자산은 기존 fill_fee 표·감사). M1은 없음.
모든 값은 가상이다.
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import date
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t053_m2_minute_backtest import Script, cfg_of, day_bars, replace_bar, utc
from tests.scenario.test_tm2_04_fees_dust import hold
from tests.scenario.test_tm2_12_backtest import RATES, config
from tests.scenario.test_tm2_15_backtest_multi import cli_args, synthetic_csv
from tests.support.invariants import check_invariants
from tests.support.m2_fakes import build_m2_env
from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2 import dust_summary, render_report_m2, run_backtest_m2
from trading_system.app.backtest_m2_cli import backtest_m2_main
from trading_system.app.backtest_m2_minute import render_minute_report, run_minute_backtest, summary_of
from trading_system.domain.enums import Mode
from trading_system.domain.fees import FeeModel
from trading_system.domain.market import CostRates
from trading_system.domain.models import OrderEvent
from trading_system.storage.repository import Repository

D = Decimal
ROOT = Path(__file__).resolve().parents[2]
SAME = FeeModel(D("0.001"), D("0.001"), "placeholder")


def fee_rows(db: Path) -> list[dict]:
    repo = Repository(db, Mode.BACKTEST)
    try:
        return repo.select("SELECT f.quantity, f.price, e.commission, e.commission_asset FROM fill_fee e "
                           "JOIN fill f ON f.fill_key = e.fill_key ORDER BY f.fill_key")
    finally:
        repo.close()


# --- 일봉 백테스트 ---------------------------------------------------------------------------------------------

def test_v336_same_rates_result_unchanged(tmp_path: Path) -> None:
    a = run_backtest_m2(config(tmp_path, run_id="a"))
    b = run_backtest_m2(config(tmp_path, run_id="b", fees=SAME))
    assert a.digest == b.digest and a.metrics == b.metrics and a.fills == b.fills and a.costs == b.costs


@pytest.mark.parametrize("maker,taker", [("0.0005", "0.002"), ("0.002", "0.0005")])
def test_v336_fill_uses_taker_estimate_uses_max(tmp_path: Path, maker: str, taker: str) -> None:
    fees = FeeModel(D(maker), D(taker), "user")
    cfg = config(tmp_path, fees=fees, cost_rates=replace(RATES, commission_rate=fees.estimate_rate))
    r = run_backtest_m2(cfg)
    rows = fee_rows(r.db_path)
    assert rows
    for x in rows:
        q, px = D(x["quantity"]), D(x["price"])
        expect = q * D(taker) if x["commission_asset"] == "TSTA" else q * px * D(taker)
        assert D(x["commission"]) == expect, x
    held = D("0")
    for f in r.fills:     # dust 집계도 체결 요율(taker)로 다시 계산
        held += D(f[4]) - D(f[4]) * D(taker) if f[3] == "BUY" else -D(f[4])
    assert dust_summary(r.fills, cfg)[1] == (held if held > 0 else D("0"))


def test_v336_estimate_mismatch_refused(tmp_path: Path) -> None:
    cfg = config(tmp_path, fees=FeeModel(D("0.001"), D("0.002"), "user"))   # cost_rates 0.001 ≠ max 0.002
    with pytest.raises(BacktestError, match="추정"):
        run_backtest_m2(cfg)


def test_v336_report_rates_source_and_placeholder_warning(tmp_path: Path) -> None:
    cfg = config(tmp_path, fees=SAME)
    report = render_report_m2(cfg, run_backtest_m2(cfg), reproducible=True, lookahead_bad=[])
    assert "maker 0.001" in report and "taker 0.001" in report and "placeholder" in report
    assert "요율 자리값" in report
    fees = FeeModel(D("0.0008"), D("0.001"), "user")
    cfg2 = config(tmp_path, run_id="u", fees=fees)
    report2 = render_report_m2(cfg2, run_backtest_m2(cfg2), reproducible=True, lookahead_bad=[])
    assert "maker 0.0008" in report2 and "taker 0.001" in report2 and "user" in report2
    assert "요율 자리값" not in report2
    cfg3 = config(tmp_path, run_id="old")
    report3 = render_report_m2(cfg3, run_backtest_m2(cfg3), reproducible=True, lookahead_bad=[])
    assert "- 비용: 수수료율 0.001(받는 자산에서 차감, D-101)" in report3 and "자리값" not in report3


def test_v336_cli_reads_fee_model_from_config(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    data = synthetic_csv(tmp_path / "BTCUSDT-1d.csv", date(2025, 3, 1), 10)
    assert backtest_m2_main(cli_args(data, tmp_path / "o", "d1"), ROOT) == 0, capsys.readouterr().out
    s = json.loads((tmp_path / "o" / "d1" / "summary.json").read_text(encoding="utf-8"))
    assert s["fee_rate"] == "0.001"                                     # 체결 요율(현 설정 = 기존 값)
    assert s["fees"] == {"maker_rate": "0.001", "taker_rate": "0.001", "rate_source": "placeholder",
                         "estimate_rate": "0.001"}
    report = (tmp_path / "o" / "d1" / "report.md").read_text(encoding="utf-8")
    assert "요율 자리값" in report


# --- 1분 봉 백테스트 -------------------------------------------------------------------------------------------

def test_v336_minute_same_rates_unchanged_and_taker_applied(tmp_path: Path) -> None:
    bars = replace_bar(day_bars(), utc(2, 0), open=D("110.00"), high=D("110.5"), close=D("110.00"))
    a = run_minute_backtest(cfg_of(tmp_path, Script(), bars, run_id="a"))
    b = run_minute_backtest(replace(cfg_of(tmp_path, Script(), bars, run_id="b"), fees=SAME))
    assert a.result.digest == b.result.digest and a.result.metrics == b.result.metrics
    fees = FeeModel(D("0.0001"), D("0.002"), "placeholder")
    c_cfg = replace(cfg_of(tmp_path, Script(), bars, run_id="c"), fees=fees,
                    cost_rates=CostRates(D("0.002"), D("0"), D("0.00000001")))
    c = run_minute_backtest(c_cfg)
    rows = fee_rows(c.result.db_path)
    buy = next(x for x in rows if x["commission_asset"] == "BTC")
    assert D(buy["commission"]) == D(buy["quantity"]) * D("0.002")
    report = render_minute_report(c_cfg, c, reproducible=None, lookahead_bad=None)
    assert "maker 0.0001" in report and "taker 0.002" in report and "요율 자리값" in report
    s = summary_of(c_cfg, c, reproducible=None, lookahead_bad=None)
    assert s["fee_rate"] == "0.002" and s["fees"]["rate_source"] == "placeholder"
    assert "fees" not in summary_of(cfg_of(tmp_path, Script(), bars, run_id="a"), a, reproducible=None,
                                    lookahead_bad=None)


# --- PAPER isMaker 기록 칸 (D-267 (4)) --------------------------------------------------------------------------

def test_fill_audit_records_is_maker_when_reported(tmp_path: Path) -> None:
    env = build_m2_env(tmp_path)
    try:
        original = env.broker.poll_events

        def with_maker() -> list[OrderEvent]:
            return [replace(e, is_maker=False) if e.kind == "FILL" else e for e in original()]

        env.broker.poll_events = with_maker  # type: ignore[method-assign]
        hold(env, "0.1000")
        [audit] = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "FILL"]
        assert audit["is_maker"] is False and audit["commission_asset"] == "TSTA"
        env.broker.poll_events = original  # type: ignore[method-assign]
        hold(env, "0.0500")
        audits = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "FILL"]
        assert "is_maker" not in audits[-1]                     # 통보에 isMaker가 없으면 칸을 만들지 않는다
        assert check_invariants(env.repo.connection, Mode.BACKTEST.value) == []
    finally:
        env.repo.close()
