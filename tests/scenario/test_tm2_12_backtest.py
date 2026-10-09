"""T-M2-12 백테스트 M2 (04 D-071~D-077·2-M2.2, CP-024 §2.6·D-101·D-104·D-108, 승인 A-066·A-071).

검증: M2 체결 모델(IOC 매수 모사·매도 슬리피지·가격제한 없음·stepSize·받는 자산 수수료·매도세 0), 가상 전략으로 M2 백테스트
실행·보고서, V-90(as-of: 확정 봉만)·V-142(진행 중 1d 봉 미사용)·V-91(교란 검사)·V-92(재현성), V-152(M1 백테스트 불변 — 기존
M1 시험이 담당). 슬리피지 값은 사용자 결정 항목(▢) — 여기 값(0.001)은 시험용 가상값이다. 모든 데이터·규칙·전략은 가상.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.m2_fakes import m2_limits, symbol_rules
from trading_system.app import backtest_m2 as bm
from trading_system.app.backtest import BacktestError
from trading_system.app.backtest_m2 import (
    SpotBacktestConfig, final_bars, perturbation_check_m2, render_report_m2, run_backtest_m2, sellable,
)
from trading_system.broker.fake.spot_fill_model import SpotBacktestBroker
from trading_system.domain.enums import Mode, OrderType, RequestType, SubmitOutcome
from trading_system.domain.market import CostRates
from trading_system.domain.models import OrderRequest, Signal
from trading_system.domain.strategy import Bar, EvaluationSnapshot, ParamSpec, RuleSpec, StrategyLimits, StrategySpec
from trading_system.storage.repository import Repository
from trading_system.strategy.base import ParamValues, make_signal

D = Decimal
SYM = "TSTA-USDT"
PAIRS = {SYM: ("TSTA", "USDT")}
RATES = CostRates(commission_rate=D("0.001"), sell_tax_rate=D("0"), cost_quantum=D("0.00000001"))
SLIP = D("0.001")   # 시험용 가상 슬리피지 (사용자 결정 값 아님)


def spec() -> StrategySpec:
    return StrategySpec(
        strategy_id="tm212-cross", name="M2 시험용 교차 전략", description="가상 규칙. 시험 전용", version=1,
        markets=("M2",), universe_method="U-A", universe=(SYM,),
        capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"),
        data_requirements="UTC 일봉 종가 1개", trigger="CAP-TRG-01 매일 00:05 UTC",
        rules=(RuleSpec("EXIT-1", "EXIT", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01", "보유 AND 종가 < exit",
                        "청산 전량(청산 가능 수량)", "데이터 없음", 1),
               RuleSpec("ENTRY-1", "ENTRY", ("CAP-DAT-01", "상태 입력"), "CAP-TRG-01", "보유 없음 AND 종가 > entry",
                        "진입 qty", "데이터 없음", 1)),
        params=(ParamSpec("entry", "decimal", "USDT", "진입선"), ParamSpec("exit", "decimal", "USDT", "청산선"),
                ParamSpec("qty", "decimal", "TSTA", "수량")),
        signal_format="00 §2.5", validity_rule="판정 + 30분", validity_max_minutes=30, unfilled_policy="시스템 기본",
        sizing="S-4", holding="수 일", limits=StrategyLimits())


class Cross:
    """마지막 확정 일봉 종가로만 판단 (진행 중 봉은 스냅샷에 없다)."""

    def __init__(self) -> None:
        self.spec = spec()
        self.seen: list[tuple[datetime, date | None]] = []

    def evaluate(self, snapshot: EvaluationSnapshot, params: ParamValues) -> list[Signal]:
        out: list[Signal] = []
        for v in snapshot.instruments:
            last = v.daily_bars[-1] if v.daily_bars else None
            self.seen.append((snapshot.as_of, last.at if last else None))  # type: ignore[arg-type]
            if last is None or v.last_price is None:
                continue
            pos = v.position
            if pos is not None and pos.owned_by_me:
                if last.close < D(str(params["exit"])):
                    out.append(make_signal(self.spec, snapshot, v.instrument, "EXIT-1", RequestType.EXIT,
                                           pos.quantity, OrderType.MARKET, v.last_price, timedelta(minutes=30)))
            elif pos is None and last.close > D(str(params["entry"])):
                out.append(make_signal(self.spec, snapshot, v.instrument, "ENTRY-1", RequestType.ENTRY,
                                       D(str(params["qty"])), OrderType.LIMIT, v.last_price, timedelta(minutes=30)))
        return out


def bars(closes: list[str], start: date = date(2025, 3, 1)) -> list[Bar]:
    """가상 UTC 일봉: 시가 = 전일 종가(첫날은 종가), 고가·저가 ±2%, 거래량 1000."""
    out: list[Bar] = []
    prev = D(closes[0])
    for i, c in enumerate(closes):
        close = D(c)
        o = prev
        out.append(Bar(start + timedelta(days=i), o, max(o, close) * D("1.02"), min(o, close) * D("0.98"), close,
                       D("1000")))
        prev = close
    return out


CLOSES = ["100", "105", "110", "112", "108", "95", "90", "92", "101", "103", "99", "94"]


def config(tmp_path: Path, **over: object) -> SpotBacktestConfig:
    rules = replace(symbol_rules(SYM), tick_size=D("0.01"), min_price=D("0.01"), lot_step=D("0.0001"),
                    lot_min_qty=D("0.0010"), market_step=D("0.0001"), market_min_qty=D("0.0010"), min_notional=D("5"))
    base = dict(run_id="m2bt", strategy=Cross(), params={"entry": "104", "exit": "100", "qty": "1.2345"},
                account_limits=m2_limits(l2_max_symbol_ratio=D("0.5"), l1_max_loss_ratio=D("0.5")),
                rules={SYM: rules}, pairs=PAIRS, cost_rates=RATES, data={SYM: bars(CLOSES)},
                start=date(2025, 3, 1), end=date(2025, 3, 12), initial_cash=D("1000"), out_dir=tmp_path,
                slippage=SLIP)
    base.update(over)
    return SpotBacktestConfig(**base)  # type: ignore[arg-type]


# --- 체결 모델 (2-M2.2) -------------------------------------------------------------------------------------

def model(slip: Decimal = SLIP) -> SpotBacktestBroker:
    return SpotBacktestBroker(pairs=PAIRS, fee_rate=D("0.001"), slippage=slip, tick_of=lambda i: D("0.01"),
                              step_of=lambda i: D("0.0001"))


def order(side: str, otype: OrderType, qty: str, price: str) -> OrderRequest:
    return OrderRequest(kind="NEW", account="BN-BT-01", instrument=SYM, side=side, quantity=D(qty), order_type=otype,
                        price=D(price))


AT = datetime(2025, 3, 2, 0, 5, tzinfo=UTC)


def test_ioc_buy_fills_only_at_or_below_limit_with_base_fee() -> None:
    m = model()
    m.place_order(order("BUY", OrderType.LIMIT, "1.0000", "100.50"))   # 시가 100 × 1.005
    m.match(SYM, D("100"), D("1000"), AT)
    [fill] = m.fills_for("BN-BT-01")
    assert fill.price == D("100.10")                                  # 100 × 1.001 → tick 올림
    assert (fill.commission, fill.commission_asset) == (D("0.0010000"), "TSTA")   # 받는 자산(기초)에서
    ev = [e for e in m.poll_events() if e.kind == "FILL"][0]
    assert ev.quote_qty == D("100.10") and ev.quote_confirmed


def test_ioc_buy_expires_when_slippage_exceeds_buffer() -> None:
    m = model(D("0.006"))   # 슬리피지 > 완충 → 체결가 > 지정가
    m.place_order(order("BUY", OrderType.LIMIT, "1.0000", "100.50"))
    m.match(SYM, D("100"), D("1000"), AT)
    assert m.fills_for("BN-BT-01") == []
    assert [e.kind for e in m.poll_events()] == ["EXPIRED"]


def test_market_sell_slippage_round_down_and_quote_fee_no_tax() -> None:
    m = model()
    m.place_order(order("SELL", OrderType.MARKET, "0.5000", "0"))
    m.match(SYM, D("100.07"), D("1000"), AT)
    [fill] = m.fills_for("BN-BT-01")
    assert fill.price == D("99.96")                      # 100.07 × 0.999 = 99.96993 → 내림
    assert (fill.commission, fill.commission_asset) == (D("0.04998000"), "USDT")   # 금액 × 요율, 매도세 0


def test_no_price_limits_and_market_buy_rejected() -> None:
    m = model()
    assert m.place_order(order("BUY", OrderType.MARKET, "1.0000", "0")).outcome is SubmitOutcome.REJECTED
    m.place_order(order("SELL", OrderType.MARKET, "1.0000", "0"))
    m.match(SYM, D("40"), D("1000"), AT)                # 전일 대비 −60%여도 하한가 없음
    assert m.fills_for("BN-BT-01")[0].price == D("39.96")


def test_volume_cap_floors_to_step_and_rest_expires() -> None:
    m = model()
    m.place_order(order("SELL", OrderType.MARKET, "5.0000", "0"))
    m.match(SYM, D("100"), D("12.34567"), AT)            # 10% = 1.234567 → step 내림 1.2345
    assert m.fills_for("BN-BT-01")[0].quantity == D("1.2345")
    assert m.poll_events()[-1].kind == "EXPIRED"


def test_sellable_quantity_floors_and_dust_is_zero(tmp_path: Path) -> None:
    r = config(tmp_path).rules[SYM]
    assert sellable(D("1.2332655"), r) == D("1.2332")      # stepSize 내림 (D-118)
    assert sellable(D("0.0009999"), r) == 0                 # minQty 0.0010 미만 = dust → 미보유 (D-102)


def test_slippage_must_be_given_and_valid(tmp_path: Path) -> None:
    with pytest.raises(TypeError):
        SpotBacktestConfig(run_id="x", strategy=Cross(), params={}, account_limits=m2_limits(), rules={},  # type: ignore[call-arg]
                           pairs=PAIRS, cost_rates=RATES, data={}, start=date(2025, 3, 1), end=date(2025, 3, 2),
                           initial_cash=D("1"), out_dir=tmp_path)
    with pytest.raises(BacktestError):
        run_backtest_m2(config(tmp_path, slippage=D("-0.1")))


# --- 실행·보고서 ----------------------------------------------------------------------------------------------

def test_run_m2_backtest_with_virtual_strategy(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    r = run_backtest_m2(cfg)
    assert len(r.days) == 12 and r.fills, r.rejects
    buys = [f for f in r.fills if f[3] == "BUY"]
    sells = [f for f in r.fills if f[3] == "SELL"]
    assert buys and sells
    # 첫 진입: 03-02 판정(03-01 확정 종가 100 < 104 → 없음), 03-03 판정(03-02 종가 105 > 104) → 03-03 시가 105 체결
    assert buys[0][0] == date(2025, 3, 3) and D(buys[0][5]) == D("105.11")   # 105 × 1.001 = 105.105 → 올림
    # 청산 수량 = 보유(1.2345 − 수수료 0.0012345 = 1.2332655)의 stepSize 내림 = 1.2332 (남은 dust는 미보유 취급)
    assert D(sells[0][4]) == D("1.2332")
    assert r.metrics["trades"] == str(len(r.trades)) and len(r.trades) >= 1
    repo = Repository(r.db_path, Mode.BACKTEST)
    try:
        fee = repo.select("SELECT commission_asset FROM fill_fee")
        assert {x["commission_asset"] for x in fee} == {"TSTA", "USDT"}
    finally:
        repo.close()
    report = render_report_m2(cfg, r, reproducible=True, lookahead_bad=[])
    assert "슬리피지 0.001" in report and "B-U4" in report and "365일" in report


def test_v92_reproducible_and_sensitive_to_slippage(tmp_path: Path) -> None:
    a = run_backtest_m2(config(tmp_path, run_id="a"))
    b = run_backtest_m2(config(tmp_path, run_id="b"))
    c = run_backtest_m2(config(tmp_path, run_id="c", slippage=D("0.003")))
    assert a.digest == b.digest and a.digest != c.digest


def test_same_run_id_refused(tmp_path: Path) -> None:
    run_backtest_m2(config(tmp_path))
    with pytest.raises(BacktestError):
        run_backtest_m2(config(tmp_path))


def test_final_period_overlap_refused(tmp_path: Path) -> None:
    with pytest.raises(BacktestError):
        run_backtest_m2(config(tmp_path, final_from=date(2025, 3, 10)))


# --- V-90·V-142·V-91 룩어헤드 --------------------------------------------------------------------------------

def test_v142_in_progress_daily_bar_never_given() -> None:
    data = bars(CLOSES)
    at = datetime(2025, 3, 5, 0, 5, tzinfo=UTC)
    assert [b.at for b in final_bars(data, at)][-1] == date(2025, 3, 4)          # 03-05 봉은 진행 중
    assert [b.at for b in final_bars(data, datetime(2025, 3, 6, 0, 0, tzinfo=UTC))][-1] == date(2025, 3, 5)
    assert [b.at for b in final_bars(data, datetime(2025, 3, 5, 23, 59, 59, tzinfo=UTC))][-1] == date(2025, 3, 4)


def test_v90_strategy_only_sees_final_bars_during_run(tmp_path: Path) -> None:
    cfg = config(tmp_path)
    run_backtest_m2(cfg)
    seen = cfg.strategy.seen   # type: ignore[attr-defined]
    assert seen and all(last is None or last < as_of.date() for as_of, last in seen)
    assert all(last == as_of.date() - timedelta(days=1) for as_of, last in seen if last is not None)


CHECK_DAYS = [date(2025, 3, 2), date(2025, 3, 3), date(2025, 3, 6), date(2025, 3, 9)]


def test_v91_perturbation_check_passes_for_causal_strategy(tmp_path: Path) -> None:
    assert perturbation_check_m2(config(tmp_path), CHECK_DAYS) == []


def test_v91_perturbation_check_detects_in_progress_bar_leak(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """검사가 실제로 누출을 잡는지: as-of 필터를 일부러 망가뜨려(진행 중 봉 포함) 같은 전략을 돌리면 03-02가 보고된다
    (03-02 봉: 시가 100 → 종가 105 — 진행 중 봉 종가를 보면 진입, 교란(종가 = 시가)하면 진입 없음)."""
    monkeypatch.setattr(bm, "final_bars", lambda data, as_of: tuple(b for b in data if b.at <= as_of.date()))
    assert "2025-03-02" in perturbation_check_m2(config(tmp_path), CHECK_DAYS)


