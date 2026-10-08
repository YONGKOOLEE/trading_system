"""T-026 검증 실행기 (CP-030 A-082: D-161~D-168·D-170, V-221~V-231).

모든 가격·달력·종목은 가상이다(형식·동작 시험용, 실제 시장 데이터 아님).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from datetime import date, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_backtest_multi_eval import TWO, cfg_p0
from tests.scenario.test_t009_backtest import CAL, DATA, INFOS, SETTINGS, config
from tests.support.gate_fakes import scenario_limits
from trading_system.app.backtest import run_backtest
from trading_system.app.validation import (
    FINAL_FROM, ScheduledTrade, TradeTemplate, ValidationError, ValidationInputs, Window, RandomEntryStrategy,
    buy_and_hold, check_final_guard, claim_final_check, decide_verdict, is_verification_strategy, param_variants,
    percentile, random_schedule, run_final_check, run_validation, run_window, walkforward_windows,
)
from trading_system.broker.fake.fill_model import BacktestBroker, BarFacts
from trading_system.config.strategy_params import validate_params
from trading_system.domain.clock import KST
from trading_system.domain.enums import OrderType
from trading_system.domain.market import CostRates
from trading_system.domain.models import OrderRequest
from trading_system.domain.strategy import ParamSpec
from trading_system.marketdata.quality import RawDaily
from trading_system.strategy.paper0_verify import Paper0Verify
from trading_system.strategy.trend_sma import TrendSmaV1

from datetime import UTC, datetime

AT = datetime(2026, 1, 5, 9, 0, tzinfo=KST).astimezone(UTC)
W1 = Window(1, date(2025, 12, 1), date(2025, 12, 31), date(2026, 1, 1), date(2026, 1, 31))
W2 = Window(2, date(2026, 1, 1), date(2026, 1, 31), date(2026, 2, 1), date(2026, 3, 31))


# --- V-221 워크포워드 창 계산 ----------------------------------------------------------------------

def test_v221_windows_follow_d162_formula() -> None:
    ws = walkforward_windows()
    assert len(ws) == 7
    for w in ws:
        k = w.k
        assert (w.train_start, w.train_end) == (date(2016 + k - 1, 1, 1), date(2018 + k - 1, 12, 31))
        assert (w.val_start, w.val_end) == (date(2019 + k - 1, 1, 1), date(2019 + k - 1, 12, 31))
    assert ws[0].val_start == date(2019, 1, 1) and ws[-1].val_end == date(2025, 12, 31)
    assert all(w.val_end < FINAL_FROM for w in ws) and FINAL_FROM == date(2026, 1, 1)


def test_v221_window_run_uses_prior_bars_only_as_warmup(tmp_path: Path) -> None:
    r = run_window(config(tmp_path), W2, label="base", out_dir=tmp_path / "out", slippage_pct=Decimal("0.001"))
    assert r.days[0] >= W2.val_start and r.days[-1] <= W2.val_end
    assert all(f[0] >= W2.val_start for f in r.fills)              # 창 시작 전 체결 없음
    assert all(t.entry_day >= W2.val_start for t in r.trades)
    # 창 첫날 판정이 직전 봉(워밍업)을 본다: 첫날 신호가 있다(가상 데이터 TST-B는 늘 진입 기준선 위)
    assert any(k.startswith(r.days[0].isoformat()) and v for k, v in r.signals_by_eval.items())


# --- V-222 워크포워드 룩어헤드 ---------------------------------------------------------------------

def test_v222_perturbing_data_after_window_end_does_not_change_window(tmp_path: Path) -> None:
    base = run_window(config(tmp_path), W1, label="a", out_dir=tmp_path / "o1", slippage_pct=Decimal("0.001"))
    wild = {i: [RawDaily(r.day, r.open * 3, r.high * 3, r.low * 3, r.close * 3, r.volume * 7, r.corporate_action)
                if r.day > W1.val_end else r for r in raw] for i, raw in DATA.items()}
    other = run_window(config(tmp_path, data=wild), W1, label="b", out_dir=tmp_path / "o2",
                       slippage_pct=Decimal("0.001"))
    assert (base.digest, base.metrics, base.fills) == (other.digest, other.metrics, other.fills)


# --- V-223 최종 구간 보호 --------------------------------------------------------------------------

def test_v223_final_period_refused_without_option(tmp_path: Path) -> None:
    check_final_guard(date(2025, 1, 1), date(2025, 12, 31), FINAL_FROM, final_check=False)
    for s, e in ((date(2025, 6, 1), date(2026, 1, 1)), (date(2026, 2, 1), date(2026, 3, 1))):
        with pytest.raises(ValidationError, match="최종 확인"):
            check_final_guard(s, e, FINAL_FROM, final_check=False)
    with pytest.raises(ValidationError, match="최종 확인 구간만"):
        check_final_guard(date(2025, 12, 1), date(2026, 3, 1), FINAL_FROM, final_check=True)
    check_final_guard(date(2026, 1, 1), date(2026, 3, 1), FINAL_FROM, final_check=True)


def test_v223_validation_refuses_window_overlapping_final(tmp_path: Path) -> None:
    inputs = ValidationInputs(template=config(tmp_path), out_dir=tmp_path / "v", report_version=1,
                              windows=(W1,), final_from=date(2026, 1, 20), random_runs=0)
    with pytest.raises(ValidationError, match="최종 확인"):
        run_validation(inputs)
    assert not (tmp_path / "v").exists()


def test_v223_final_check_once_per_version_and_recorded(tmp_path: Path) -> None:
    ledger = tmp_path / "final-usage.json"
    cfg = config(tmp_path)
    with pytest.raises(ValidationError, match="확정"):
        claim_final_check(ledger, cfg.strategy.spec, "fc1", confirmed_by="")
    out = run_final_check(cfg, tmp_path / "fc", final_from=date(2026, 2, 1), end=date(2026, 3, 31), ledger=ledger,
                          confirmed_by="A-999", slippage_pct=Decimal("0.001"))
    assert out["start"] >= "2026-02-01"
    recs = [json.loads(x) for x in ledger.read_text(encoding="utf-8").splitlines()]
    assert recs[0]["strategy"] == "t-cross" and recs[0]["version"] == 1 and recs[0]["confirmed_by"] == "A-999"
    with pytest.raises(ValidationError, match="한 번만"):
        run_final_check(cfg, tmp_path / "fc2", final_from=date(2026, 2, 1), end=date(2026, 3, 31), ledger=ledger,
                        confirmed_by="A-999", slippage_pct=Decimal("0.001"))


def test_v223_final_check_shares_backtest_final_ledger(tmp_path: Path) -> None:
    """기존 60/20/20 FINAL 사용 기록이 있으면 최종 확인도 거부 (같은 장부, 전략 버전당 1회)."""
    ledger = tmp_path / "final-usage.json"
    ledger.write_text(json.dumps({"strategy": "t-cross", "version": 1, "run_id": "old"}) + "\n", encoding="utf-8")
    with pytest.raises(ValidationError, match="한 번만"):
        claim_final_check(ledger, config(tmp_path).strategy.spec, "x", confirmed_by="A-1")


# --- V-224 슬리피지 % -----------------------------------------------------------------------------

def _pct_broker(pct: str, unit: str = "1") -> BacktestBroker:
    return BacktestBroker(lambda i, p: Decimal("10"), slippage_pct=Decimal(pct),
                          price_unit=lambda i, p: Decimal(unit))


def _fill_prices(b: BacktestBroker, bar: BarFacts) -> dict[str, Decimal]:
    for side in ("BUY", "SELL"):
        b.place_order(OrderRequest("NEW", "A", "TST-A", side, Decimal("1"), OrderType.MARKET, Decimal("1")))
    b.match_bar("TST-A", bar, AT)
    ev = b.poll_events()
    return {b.side_of(e.order_no) or "": e.price for e in ev}  # type: ignore[misc]


def _bar(o: str, up: str | None = None, low: str | None = None) -> BarFacts:
    p = Decimal(o)
    return BarFacts(p, p * 2, p / 2, Decimal("1000"), None if up is None else Decimal(up),
                    None if low is None else Decimal(low))


def test_v224_percent_slippage_adverse_and_double_width() -> None:
    assert _fill_prices(_pct_broker("0.001"), _bar("10000")) == {"BUY": Decimal("10010"), "SELL": Decimal("9990")}
    assert _fill_prices(_pct_broker("0.002"), _bar("10000")) == {"BUY": Decimal("10020"), "SELL": Decimal("9980")}
    # 원 단위 불리한 방향: 12345×1.001 = 12357.345 → 12358, 12345×0.999 = 12332.655 → 12332
    assert _fill_prices(_pct_broker("0.001"), _bar("12345")) == {"BUY": Decimal("12358"), "SELL": Decimal("12332")}
    # 호가 단위(가상 50원) 불리한 방향: 12400 / 12300
    assert _fill_prices(_pct_broker("0.001", "50"), _bar("12345")) == {"BUY": Decimal("12400"),
                                                                      "SELL": Decimal("12300")}
    assert _fill_prices(_pct_broker("0"), _bar("12345")) == {"BUY": Decimal("12345"), "SELL": Decimal("12345")}


def test_v224_price_limit_rules_kept_with_percent() -> None:
    # 상한가 근처: 매수 체결가는 상한가로 제한, 하한가 근처: 매도 체결가는 하한가로 제한
    assert _fill_prices(_pct_broker("0.001"), _bar("10000", up="10005", low="9995")) == {
        "BUY": Decimal("10005"), "SELL": Decimal("9995")}
    # 시가 = 상한가 → 매수 미체결, 시가 = 하한가 → 매도 미체결
    assert "BUY" not in _fill_prices(_pct_broker("0.001"), _bar("10000", up="10000"))
    assert "SELL" not in _fill_prices(_pct_broker("0.001"), _bar("10000", low="10000"))


def test_v224_percent_slippage_rejects_bad_values() -> None:
    for bad in ("-0.001", "1"):
        with pytest.raises(ValueError):
            BacktestBroker(lambda i, p: Decimal("10"), slippage_pct=Decimal(bad), price_unit=lambda i, p: Decimal(1))


def test_v224_backtest_uses_percent_when_set(tmp_path: Path) -> None:
    r = run_backtest(config(tmp_path, "pct", slippage_pct=Decimal("0.001")))
    bars = {i: {b.day: b for b in raw} for i, raw in DATA.items()}
    assert r.fills
    for day, _no, inst, side, _q, price in r.fills:
        o = bars[inst][day].open
        raw = o * Decimal("1.001") if side == "BUY" else o * Decimal("0.999")
        want = raw.to_integral_value(rounding="ROUND_CEILING" if side == "BUY" else "ROUND_FLOOR")
        assert Decimal(price) == want, (day, inst, side)
    s = json.loads((r.db_path.parent / "summary.json").read_text(encoding="utf-8"))
    assert s["slippage_pct"] == "0.001"


# --- V-225 파라미터 변형 ---------------------------------------------------------------------------

def test_v225_trend_sma_variants() -> None:
    strat = TrendSmaV1()
    params = validate_params(strat.spec, {"fast": 20, "slow": 60, "atr_n": 14, "atr_mult": "2", "reentry_wait": 5,
                                          "risk_ratio": "0.004", "max_weight": "0.09"})
    vs = param_variants(strat.spec, params)
    assert len(vs) == 2 * 7                                     # 실행 수 = 2P
    got = {(v.param, v.direction): v.value for v in vs}
    assert got[("fast", "+")] == 24 and got[("fast", "-")] == 16
    assert got[("atr_n", "+")] == 17 and got[("atr_n", "-")] == 11        # 16.8 → 17, 11.2 → 11
    assert got[("atr_mult", "+")] == Decimal("2.4") and got[("atr_mult", "-")] == Decimal("1.6")
    assert got[("max_weight", "+")] == Decimal("0.10")                   # 0.108 → 허용 상한 0.10
    assert got[("max_weight", "-")] == Decimal("0.072")
    clamp = next(v for v in vs if v.param == "max_weight" and v.direction == "+")
    assert "경계" in clamp.note
    for v in vs:                                                # 변형 파라미터는 명세 검사를 통과하고 하나만 바뀜
        assert sum(1 for k in params if v.params[k] != params[k]) <= 1
        validate_params(strat.spec, {k: (str(x) if isinstance(x, Decimal) else x) for k, x in v.params.items()})


def test_v225_int_same_value_uses_plus_minus_one_and_bounds() -> None:
    from tests.support.sample_strategies import cross_spec
    spec = replace(cross_spec(), params=(
        ParamSpec("n", "int", "일", "작은 정수", Decimal("0"), Decimal("3")),
        ParamSpec("m", "int", "일", "경계", Decimal("0"), Decimal("10")),
        ParamSpec("name", "str", "", "문자열", choices=("a", "b"))))
    vs = param_variants(spec, {"n": 2, "m": 0, "name": "a"})
    got = {(v.param, v.direction): (v.value, v.note) for v in vs}
    assert len(vs) == 4                                          # 수치 파라미터 2개 × 2 (문자열 제외)
    assert got[("n", "+")][0] == 3 and "±1" in got[("n", "+")][1]   # 2.4 → 2 (같은 값) → 3
    assert got[("n", "-")][0] == 1 and "±1" in got[("n", "-")][1]   # 1.6 → 2 (같은 값) → 1
    assert got[("m", "+")][0] == 1
    assert got[("m", "-")][0] == 0 and "경계" in got[("m", "-")][1]  # 0 → −1 → 하한 0 (기준값과 같음)


# --- V-226 단순 보유 -------------------------------------------------------------------------------

def test_v226_buy_and_hold_hand_calculation() -> None:
    d1, d2, d3 = date(2026, 1, 5), date(2026, 1, 6), date(2026, 1, 7)
    data = {"A": [RawDaily(d1, Decimal("10000"), Decimal("10000"), Decimal("10000"), Decimal("10500"), Decimal("1")),
                  RawDaily(d3, Decimal("12000"), Decimal("12000"), Decimal("12000"), Decimal("12000"), Decimal("1"))],
            "B": [RawDaily(d1, Decimal("20000"), Decimal("20000"), Decimal("20000"), Decimal("19000"), Decimal("1")),
                  RawDaily(d2, Decimal("18000"), Decimal("18000"), Decimal("18000"), Decimal("18000"), Decimal("1"))],
            "C": [RawDaily(d2, Decimal("5000"), Decimal("5000"), Decimal("5000"), Decimal("5000"), Decimal("1"))]}
    r = buy_and_hold(data, [d1, d2, d3], Decimal("1000000"), Decimal("0.001"),
                     CostRates(Decimal("0.00015"), Decimal("0.0018")), lambda i, p: Decimal("1"))
    # 첫날 봉 있는 A·B만, 종목당 500,000원: A 10010×49 = 490,490 + 비용 74, B 20020×24 = 480,480 + 비용 73
    # 현금 28,883 + A 49×12,000 + B 24×18,000(마지막 봉 종가) = 1,048,883
    # [재검토 M-4, 2026-10-08] 창 끝 매도 비용(수수료+세금 0.00195, 원 미만 올림)도 반영:
    #   A 588,000 → 1,147, B 432,000 → 843 → 1,048,883 − 1,990 = 1,046,893 (이전 기대값 1,048,883은 매도 비용 미반영)
    assert r["instruments"] == ["A", "B"] and r["excluded"] == ["C"]
    assert r["final_equity"] == "1046893" and r["total_return"] == "0.046893"
    assert r["sell_costs"] == "1990"


# --- V-227 무작위 진입 -----------------------------------------------------------------------------

def _days() -> list[date]:
    return [d for d in (date(2026, 1, 5) + timedelta(days=i) for i in range(80)) if CAL.is_trading_day(d)]


def test_v227_schedule_same_seed_same_and_matches_distribution() -> None:
    days = _days()
    bars_on = {i: {r.day for r in raw} for i, raw in DATA.items()}
    tpl = [TradeTemplate(h, Decimal("100000"), Decimal("0.05")) for h in (1, 3, 3, 7, 2)]
    a, skip_a = random_schedule("seed:1", days, bars_on, tpl, sorted(DATA))
    b, _ = random_schedule("seed:1", days, bars_on, tpl, sorted(DATA))
    c, _ = random_schedule("seed:2", days, bars_on, tpl, sorted(DATA))
    assert a == b and a != c and skip_a == 0
    assert sorted(t.holding_days for t in a) == [1, 2, 3, 3, 7]   # 보유 기간 분포 = 전략
    for t in a:
        assert days.index(t.exit_day) - days.index(t.entry_day) == t.holding_days
    for inst in DATA:                                             # 같은 종목 구간 겹침 없음
        iv = sorted((t.entry_day, t.exit_day) for t in a if t.instrument == inst)
        assert all(x[1] < y[0] for x, y in zip(iv, iv[1:]))


def test_v227_random_entry_goes_through_gate_limits(tmp_path: Path) -> None:
    base = config(tmp_path)
    days = [d for d in _days() if d <= date(2026, 3, 31)]
    sched = (ScheduledTrade("TST-A", days[2], days[8], Decimal("200000"), None, 6),
             ScheduledTrade("TST-B", days[4], days[6], Decimal("200000"), None, 2))   # TST-A 보유 중 진입
    strat = RandomEntryStrategy(base.strategy.spec, sched, (time(9, 5),))
    cfg = replace(base, run_id="rnd", strategy=strat, params={},
                  account_limits=scenario_limits(l6_max_positions=1, l7_max_order_amount=Decimal("10000000")))
    r = run_backtest(cfg)
    assert [(t.instrument, t.entry_day, t.exit_day) for t in r.trades] == [("TST-A", days[2], days[8])]
    assert r.rejects.get("G7", 0) >= 1                              # 보유 종목 수 한도(L6)로 TST-B 거부
    one = {strat.spec.strategy_id, base.strategy.spec.strategy_id}
    assert len(one) == 2 and strat.spec.strategy_id.endswith("-random")


def test_v227_percentile() -> None:
    dist = [Decimal(x) for x in ("0.01", "0.02", "0.03", "0.04")]
    assert percentile(Decimal("0.025"), dist) == Decimal("50.0")
    assert percentile(Decimal("0.02"), dist) == Decimal("37.5")
    assert percentile(Decimal("1"), dist) == Decimal("100.0") and percentile(Decimal("0"), dist) == Decimal("0.0")
    assert percentile(Decimal("0"), []) is None


# --- V-228·V-229·V-230 판정 ------------------------------------------------------------------------

def _v(**kw: object) -> dict:
    args = dict(exempt=False, pooled_trades=100, c3_expectancy=Decimal("1"), c4_expectancies=[Decimal("1")],
                after_the_fact_universe=False)
    args.update(kw)
    return decide_verdict(**args)  # type: ignore[arg-type]


def test_v228_min_trades() -> None:
    assert _v(pooled_trades=99)["verdict"] == "판단 불가"
    assert _v(pooled_trades=99, c3_expectancy=Decimal("-1"))["verdict"] == "판단 불가"
    assert _v(pooled_trades=100)["verdict"] == "통과"


def test_v229_verdict_combinations() -> None:
    assert _v(c3_expectancy=Decimal("0"))["verdict"] == "미충족"           # > 0 이어야 함
    assert "-8604.48원" in str(_v(c3_expectancy=Decimal("-8604.484076433121019108280255"))["reasons"])
    assert _v(c3_expectancy=None)["verdict"] == "미충족"
    assert _v(c4_expectancies=[Decimal("5"), Decimal("-1")])["verdict"] == "미충족"
    assert _v(c4_expectancies=[Decimal("5"), None])["verdict"] == "미충족"   # 거래 없음 변형
    assert _v()["verdict"] == "통과"
    v = _v(after_the_fact_universe=True)
    assert v["verdict"] == "통과(과대 추정 가능)" and "생존편향" in v["banner"]
    assert _v(after_the_fact_universe=True, c3_expectancy=Decimal("-1"))["verdict"] == "미충족"
    assert _v(after_the_fact_universe=True, pooled_trades=3)["banner"]


def test_v230_verification_strategy_exempt(tmp_path: Path) -> None:
    assert is_verification_strategy(Paper0Verify().spec)
    assert not is_verification_strategy(TrendSmaV1().spec)
    assert _v(exempt=True, pooled_trades=0)["verdict"].startswith("적용 제외")
    cfg = cfg_p0(tmp_path, "p0")
    out = run_validation(ValidationInputs(template=cfg, out_dir=tmp_path / "v", report_version=1,
                                          windows=(W1,), final_from=date(2027, 1, 1), random_runs=0))
    assert out["verdict"]["verdict"].startswith("적용 제외") and out["runs"] == 0
    assert not (tmp_path / "v" / "runs").exists()


# --- 실행기 종단 (D-170) ---------------------------------------------------------------------------

def test_d170_runner_end_to_end_outputs_and_determinism(tmp_path: Path) -> None:
    def go(name: str, workers: int = 1) -> dict:
        inputs = ValidationInputs(template=config(tmp_path), out_dir=tmp_path / name, report_version=1,
                                  windows=(W1, W2), final_from=date(2027, 1, 1), random_runs=3, workers=workers)
        return run_validation(inputs)

    a = go("a")
    out = tmp_path / "a"
    assert (out / "validation-v1.md").exists() and (out / "verdict.json").exists()
    for k in (1, 2):
        for label in ("base", "slip2x"):
            d = out / "runs" / f"w{k}-{label}"
            assert {"trades.csv", "equity.csv", "summary.json"} <= {p.name for p in d.iterdir()}
        s = json.loads((out / "runs" / f"w{k}-base" / "summary.json").read_text(encoding="utf-8"))
        assert s["reproducible"] is True and s["slippage_pct"] == "0.001"
    assert a["runs_by_kind"]["variants"] == 2 * 3 * 2                    # 창 2 × 2P(P=3)
    assert a["runs_by_kind"]["random"] == 3 * 2
    v = a["verdict"]
    assert v["verdict"] == "판단 불가"                                     # 가상 데이터 거래 수 < 100
    assert "생존편향" in v["banner"]                                        # 기본: 사후 선택 목록으로 본다
    md = (out / "validation-v1.md").read_text(encoding="utf-8")
    assert md.index("생존편향") < md.index("## 판정표")
    assert "합산)" + chr(10) * 2 + "- 유효 변형" in md                          # 제목 뒤 빈 줄 (M-3 줄 추가)
    assert "C-2" in md and "C-6" in md and "단순 보유" in md and "무작위 진입" in md
    b = go("b", workers=2)                                                 # 병렬 실행도 같은 결과
    strip = lambda x: {k: v for k, v in x.items() if k not in ("runtime_seconds", "out_dir")}   # noqa: E731
    assert strip(a) == strip(b)


def test_d170_runner_refuses_existing_output(tmp_path: Path) -> None:
    (tmp_path / "v").mkdir()
    with pytest.raises(ValidationError, match="이미 있음"):
        run_validation(ValidationInputs(template=config(tmp_path), out_dir=tmp_path / "v", report_version=1,
                                        windows=(W1,), final_from=date(2027, 1, 1), random_runs=0))


# --- 14:30 판정 체결 시각 (T-009 재검토 메모) ------------------------------------------------------

def test_multi_eval_1430_fill_stamps_in_trades_csv(tmp_path: Path) -> None:
    r = run_backtest(cfg_p0(tmp_path, "p0two"))
    lines = (r.db_path.parent / "trades.csv").read_text(encoding="utf-8-sig").splitlines()[1:]
    rows = [x.split(",") for x in lines]
    assert rows and all(x[1].endswith(" 09:30") and x[2].endswith(" 14:30") for x in rows)
    assert all(x[1][:10] == x[2][:10] for x in rows)                      # 같은 날 왕복
    assert TWO == (time(9, 30), time(14, 30))


# --- V-231 기존 백테스트 불변 ----------------------------------------------------------------------

def test_v231_default_backtest_unchanged(tmp_path: Path) -> None:
    """T-026 착수 전(feat/T-009-bt-export 295e74d) 코드로 계산한 값. 기본(틱 슬리피지) 결과가 그대로여야 한다."""
    r = run_backtest(config(tmp_path, "pin"))
    assert r.digest == "c5345ebbbe00d98a5c7f5dccb2f98ee994054a9482d8c2a40ce51d09cbe77c00"
    assert r.metrics["final_equity"] == "9939044" and r.metrics["trades"] == "5"
    d = r.db_path.parent
    sha = lambda p: hashlib.sha256(p.read_bytes()).hexdigest()   # noqa: E731
    assert sha(d / "trades.csv") == "629445fdcee24a5259cec7e0fc03ecfb40953e089970cf3463ff6260eb534d89"
    assert sha(d / "equity.csv") == "0ea4ace4f9be33cd014f60a548179f740b43d48a7ae5d1050a79c0eecab8bdc0"
    assert hashlib.sha256((d / "summary.json").read_text(encoding="utf-8").encode()).hexdigest() == \
        "ef7f7390122636e5f3bc581c475257a2f4b7b5ab29e686fa65bdb5a002d07cf2"
    assert run_backtest(cfg_p0(tmp_path, "pin2")).digest == \
        "45ea771d044fcb5581e6f08c408061723a138afee52d620e8702762779606457"


# --- 명령 (scripts/validate-strategy.py 본체) ------------------------------------------------------

REPO = Path(__file__).resolve().parents[2]


def test_validation_settings_file_matches_cp030() -> None:
    from trading_system.app.validation_tool import load_validation_settings
    s = load_validation_settings(REPO / "config" / "validation.toml")
    assert s == {"final_from": date(2026, 1, 1), "slippage_base": Decimal("0.001")}


def _cli_data(tmp_path: Path) -> Path:
    d = tmp_path / "data"
    d.mkdir()
    for inst, raw in DATA.items():
        lines = ["date,open,high,low,close,volume"] + [
            f"{r.day.isoformat()},{r.open},{r.high},{r.low},{r.close},{r.volume}" for r in raw]
        (d / f"{inst}.csv").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (tmp_path / "p.toml").write_text('strategy = "t-cross"\nversion = 1\n[params]\nentry_level = "9000"\n'
                                     'exit_level = "8000"\nqty = 10\n', encoding="utf-8")
    return d


def test_cli_final_check_needs_confirmation_then_once(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.validation_tool import validate_main
    d = _cli_data(tmp_path)
    ledger = tmp_path / "ledger.json"
    base = ["--strategy", "tests.support.sample_strategies:CrossStrategy", "--params", str(tmp_path / "p.toml"),
            "--data", str(d), "--report-version", "1", "--out-root", str(tmp_path / "out"), "--final-check",
            "--ledger", str(ledger), "--end", "2026-03-31"]
    assert validate_main(base, REPO) == 2 and "확정" in capsys.readouterr().out
    assert not ledger.exists()
    assert validate_main([*base, "--confirmed-by", "A-999"], REPO) == 0
    assert json.loads(ledger.read_text(encoding="utf-8").splitlines()[0])["confirmed_by"] == "A-999"
    assert (tmp_path / "out" / "t-cross-v1-final" / "final-check.json").exists()
    assert validate_main([*base, "--report-version", "2", "--confirmed-by", "A-999"], REPO) == 2
    assert "한 번만" in capsys.readouterr().out


# --- 재검토 반영 (2026-10-08 독립 검토 H-1·M-1~M-5·L-1·L-2) -----------------------------------------

from trading_system.app.backtest import BacktestConfig, BacktestError  # noqa: E402


def test_h1_backtest_default_final_from_is_cp030_date() -> None:
    import dataclasses
    f = {x.name: x for x in dataclasses.fields(BacktestConfig)}
    assert f["final_from"].default == date(2026, 1, 1) == FINAL_FROM and f["final_check"].default is False


@pytest.mark.parametrize("segment", ["ALL", "TRAIN", "VALIDATION", "FINAL"])
def test_h1_general_backtest_refuses_final_period(tmp_path: Path, segment: str) -> None:
    cfg = config(tmp_path, f"h1-{segment}", final_from=FINAL_FROM, segment=segment)   # 운영 기본값
    with pytest.raises(BacktestError, match="최종 확인"):
        run_backtest(cfg)
    assert not (tmp_path / "var" / "backtest" / f"h1-{segment}").exists()
    assert not (tmp_path / "var" / "backtest" / "final-usage.jsonl").exists()


def test_h1_run_ending_before_final_from_allowed_overlap_refused(tmp_path: Path) -> None:
    run_backtest(config(tmp_path, "before", final_from=date(2026, 2, 1), end=date(2026, 1, 30)))
    with pytest.raises(BacktestError, match="최종 확인"):
        run_backtest(config(tmp_path, "over", final_from=date(2026, 2, 1), end=date(2026, 2, 2)))


def test_h1_final_check_run_needs_ledger_record_and_final_only(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    kw = dict(final_from=date(2026, 2, 1), final_check=True, final_usage_ledger=ledger)
    with pytest.raises(BacktestError, match="장부"):
        run_backtest(config(tmp_path, "nofc", start=date(2026, 2, 2), **kw))       # 기록 없음
    claim_final_check(ledger, config(tmp_path).strategy.spec, "fc", confirmed_by="A-1")
    with pytest.raises(BacktestError, match="최종 확인 구간만"):
        run_backtest(config(tmp_path, "early", start=date(2026, 1, 5), **kw))      # 최종 구간 앞 날짜 포함
    r = run_backtest(config(tmp_path, "ok", start=date(2026, 2, 2), **kw))
    assert r.days[0] >= date(2026, 2, 1)


def test_h1_validator_overrides_template_with_its_final_from(tmp_path: Path) -> None:
    out = run_validation(ValidationInputs(template=config(tmp_path, final_from=FINAL_FROM), out_dir=tmp_path / "v",
                                          report_version=1, windows=(W1,), final_from=date(2027, 1, 1),
                                          random_runs=0))
    assert out["windows"][0]["base"]["trades"] >= 0


def test_m1_reproducibility_failure_is_undecidable() -> None:
    v = _v(reproducible=False)
    assert v["verdict"] == "판단 불가" and any("재현성" in x for x in v["reasons"])
    assert _v(reproducible=False, c3_expectancy=Decimal("-1"))["verdict"] == "판단 불가"


def test_m2_no_numeric_params_c4_undecidable() -> None:
    v = _v(c4_expectancies=[])
    assert v["C-4"] == "판단 불가" and v["verdict"] == "판단 불가"
    assert any("[제안]" in x and "C-4" in x for x in v["reasons"])
    assert _v(c4_expectancies=[], c3_expectancy=Decimal("-1"))["verdict"] == "미충족"


def _dummy_strategy_cfg(tmp_path: Path) -> object:
    from tests.support.sample_strategies import CrossStrategy, PARAMS, cross_spec
    spec = cross_spec()
    spec = replace(spec, params=spec.params + (ParamSpec("dummy", "int", "개", "효과 없는 값", Decimal("1"),
                                                         Decimal("100")),))
    strat = CrossStrategy(spec)
    return config(tmp_path, strategy=strat, params=validate_params(spec, {**PARAMS, "dummy": 10}))


def test_m3_m4_m5_tables(tmp_path: Path) -> None:
    out = run_validation(ValidationInputs(template=_dummy_strategy_cfg(tmp_path), out_dir=tmp_path / "v",  # type: ignore[arg-type]
                                          report_version=1, windows=(W1, W2), final_from=date(2027, 1, 1),
                                          random_runs=3))
    same = {(x["param"], x["direction"]): x["same_as_base"] for x in out["variants"]}
    assert same[("dummy", "+")] and same[("dummy", "-")] and not same[("qty", "+")]
    assert out["effective_variants"] == len(out["variants"]) - 2
    md = (tmp_path / "v" / "validation-v1.md").read_text(encoding="utf-8")
    assert "결과 동일(한도에 묶임 가능)" in md and f"유효 변형 {len(out['variants']) - 2}/{len(out['variants'])}" in md
    w = out["windows"][0]
    assert {"avg", "max"} <= set(w["base"]["invested_ratio"]) and "sell_costs" in w["buy_hold"]   # M-4
    assert "평균·최대 투자 비중" in md
    assert w["random"]["strategy_trades"] == w["base"]["trades"]                                   # M-5
    # [A-083, 2026-10-08] 무작위 진입은 공통 기준에서 제외(선택 실행). 이전 기대 "CP 기준 200회 미충족, 3회"는 삭제
    assert "선택 실행(공통 기준 아님, A-083), 3회" in md and "미충족, 3회" not in md and "200회" not in md
    zero = run_validation(ValidationInputs(template=_dummy_strategy_cfg(tmp_path), out_dir=tmp_path / "v0",  # type: ignore[arg-type]
                                           report_version=1, windows=(W1, W2), final_from=date(2027, 1, 1),
                                           random_runs=0))
    assert zero["verdict"] == out["verdict"]                     # 판정은 무작위 진입과 무관 (A-083)
    assert zero["runs_by_kind"]["random"] == 0
    md0 = (tmp_path / "v0" / "validation-v1.md").read_text(encoding="utf-8")
    assert "삭제(A-083) — 공통 기준 아님, 미실행" in md0 and "200회" not in md0


def test_a083_random_entry_off_by_default() -> None:
    """[A-083] 무작위 진입 비교는 공통 기준이 아니다: 기본 0회(미실행), --random-runs N으로만 실행."""
    import dataclasses
    from trading_system.app.validation import RANDOM_RUNS
    f = {x.name: x for x in dataclasses.fields(ValidationInputs)}
    assert RANDOM_RUNS == 0 and f["random_runs"].default == 0
    import inspect
    assert "random" not in str(inspect.signature(decide_verdict))


def test_l1_final_check_validates_before_ledger(tmp_path: Path) -> None:
    ledger = tmp_path / "ledger.json"
    cfg = config(tmp_path)
    for end in (date(2027, 3, 31), date(2026, 1, 15)):     # 달력 없는 연도 / 최종 구간 시작 전 종료
        with pytest.raises((ValidationError, BacktestError)):
            run_final_check(cfg, tmp_path / f"fc-{end}", final_from=date(2026, 2, 1), end=end, ledger=ledger,
                            confirmed_by="A-999")
    empty = replace(cfg, data={i: [r for r in raw if r.day < date(2026, 2, 1)] for i, raw in cfg.data.items()})
    with pytest.raises(ValidationError, match="일봉"):
        run_final_check(empty, tmp_path / "fc-e", final_from=date(2026, 2, 1), end=date(2026, 3, 31),
                        ledger=ledger, confirmed_by="A-999")
    assert not ledger.exists()
    assert not any((tmp_path / n).exists() for n in ("fc-2027-03-31", "fc-2026-01-15", "fc-e"))


def test_l2_input_errors_do_not_consume_report_version(tmp_path: Path) -> None:
    bad = Window(1, date(2023, 1, 1), date(2023, 12, 31), date(2024, 1, 1), date(2024, 12, 31))   # 달력 없음
    with pytest.raises((ValidationError, BacktestError)):
        run_validation(ValidationInputs(template=config(tmp_path), out_dir=tmp_path / "v", report_version=1,
                                        windows=(bad,), final_from=date(2027, 1, 1), random_runs=0))
    with pytest.raises(ValidationError):
        run_validation(ValidationInputs(template=config(tmp_path), out_dir=tmp_path / "v", report_version=1,
                                        windows=(W1,), final_from=date(2027, 1, 1), random_runs=0,
                                        window_params={9: {}}))
    assert not (tmp_path / "v").exists()


def test_l2_rmtree_failure_is_logged(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                     caplog: pytest.LogCaptureFixture) -> None:
    import trading_system.app.validation as val

    def fake_rmtree(path: object, onexc: object = None) -> None:
        onexc(lambda: None, str(path), PermissionError("잠김"))  # type: ignore[operator]
    monkeypatch.setattr(val.shutil, "rmtree", fake_rmtree)
    with caplog.at_level("WARNING"):
        val._remove_tree(tmp_path / "x")
    assert "잠김" in caplog.text
