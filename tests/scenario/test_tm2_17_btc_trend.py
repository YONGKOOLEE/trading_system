"""T-M2-17 후보 btc-trend v1 (전략실 → 운영 복사본, 미등록·미승인 — docs/strategies/btc-trend/validation-v1.md).

검증 범위(구조·적재만 — 성과 판정은 validation-v1.md):
- 파라미터 파일 2개(config/strategies/btc-trend-v1.toml, -paper.toml)가 명세로 적재·검증되는지.
- 명세: 규칙 표(EXIT-2 > EXIT-1, ENTRY-1), 실제 docs/capabilities.md 기준 BACKTEST 등록 통과(운영 수정 F-1 뒤),
  PAPER 등록은 M2 능력 "검증 중"이라 거부(현재 상태 기록 — 등록·PAPER 투입은 사용자 별도 승인).
- 거래 규칙(테스트넷 실측, broker-specs/binance-spot.md §12 = backtest_m2_cli.TESTNET_RULES)과 전략 고정값 LOT_STEP·PAPER 크기 정합.
- 시작 규칙 검사(D-209·DP-54 형식 validate_rules)·배포 검사(validate_deploy): 전략실 원본에 없던 결함 F-2·F-3을
  운영에서 보강(A-114 ③, 거래 논리 변경 없음) — 일치·통과 경우와 거부 경우를 확인한다.
- 규칙 동작 최소 확인(가상 봉): 돌파 진입·수량 내림·손절가, EXIT-1, EXIT-2 우선, 수동 보유 무시.
가격·잔고는 가상값이다. 네트워크 없음.
"""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.app.backtest_m2_cli import TESTNET_RULES
from trading_system.app.wiring import StrategyInput, spot_rule_problems
from trading_system.config.capabilities import load_capabilities
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_params import load_params_file
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.strategy import btc_trend
from trading_system.strategy.btc_trend import BtcTrendV1

ROOT = Path(__file__).resolve().parents[2]
D = Decimal
SYM = "BTCUSDT"
CAPS = load_capabilities(ROOT / "docs" / "capabilities.md")
RULES = TESTNET_RULES[SYM]          # 테스트넷 실측 (2026-10-14): LOT step 0.00001, minNotional 5
PAIRS = {SYM: ("BTC", "USDT")}
EVAL = (time(0, 15),)               # 운영 M2 백테스트 기본 판정 시각(UTC)
AT = datetime(2026, 10, 20, 0, 15, tzinfo=UTC)


def params(name: str) -> dict[str, object]:
    return load_params_file(BtcTrendV1().spec, ROOT / "config" / "strategies" / name)


def m2_paper_limits():  # type: ignore[no-untyped-def]
    return load_limits_file(ROOT / "config" / "m2" / "limits.paper.toml", market="M2").limits


# --- 파라미터 파일 ------------------------------------------------------------------------------------

def test_both_param_files_load_with_lab_values() -> None:
    v1, paper = params("btc-trend-v1.toml"), params("btc-trend-v1-paper.toml")
    rules_part = {"entry_n": 25, "exit_n": 50, "stop_atr": D("2")}
    assert {k: v1[k] for k in rules_part} == rules_part == {k: paper[k] for k in rules_part}   # 규칙 값은 같고 크기만 다름
    assert (v1["risk_ratio"], v1["max_weight"], v1["order_cap"]) == (D("0.05"), D("0.98"), D("1000000000"))
    assert (paper["risk_ratio"], paper["max_weight"], paper["order_cap"]) == (D("0.004"), D("0.10"), D("95"))


# --- 명세·등록 ----------------------------------------------------------------------------------------

def test_spec_rules_and_priorities() -> None:
    spec = BtcTrendV1().spec
    assert (spec.strategy_id, spec.version, spec.markets, spec.universe) == ("btc-trend", 1, ("M2",), (SYM,))
    assert [(r.rule_id, r.kind, r.priority) for r in spec.rules] == [("EXIT-2", "EXIT", 1), ("EXIT-1", "EXIT", 2),
                                                                     ("ENTRY-1", "ENTRY", 1)]
    assert spec.validity_max_minutes == 30 and spec.sizing == "S-3"


def test_registration_backtest_passes_after_f1_and_paper_refused_while_m2_caps_verifying() -> None:
    """F-1: 전략실 원본은 CAP-INS-01(국내 상장주식, M2 "해당 없음")이라 BACKTEST 등록부터 거부됐다 → CAP-INS-05로 수정.
    PAPER 등록은 M2 열 "검증 중"이라 거부(검증 전략 예외 D-212는 m2-verify 계열 전용) — 현재 상태 기록."""
    spec = BtcTrendV1().spec
    assert "CAP-INS-05" in spec.capabilities and "CAP-INS-01" not in spec.capabilities
    assert validate_spec(spec, CAPS, m2_paper_limits(), Mode.BACKTEST) == []
    refused = validate_spec(spec, CAPS, m2_paper_limits(), Mode.PAPER)
    assert refused and all("검증 중" in p for p in refused), refused
    original = replace(spec, capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"))
    assert any("CAP-INS-01" in p for p in validate_spec(original, CAPS, m2_paper_limits(), Mode.BACKTEST))


# --- 거래 규칙 정합 (테스트넷 실측) -------------------------------------------------------------------

def test_hardcoded_lot_step_matches_measured_testnet_rule() -> None:
    assert btc_trend.LOT_STEP == RULES.lot_step == D("0.00001")


@pytest.mark.parametrize("price", ["15000.00", "60000.00", "126000.00", "999999.99"])
def test_paper_size_stays_inside_l7_and_above_min_notional(price: str) -> None:
    """PAPER 축소판(order_cap 95): IOC 지정가(시세 × 1.005) 기준 주문 금액 ≤ L7 100 USDT(A-064), 내림 뒤 ≥ minNotional 5."""
    p = D(price)
    [s] = BtcTrendV1().evaluate(snapshot(breakout_bars(p), last=p), params("btc-trend-v1-paper.toml"))
    assert s.quantity * p * D("1.005") <= m2_paper_limits().l7_max_order_amount
    assert s.quantity * p >= RULES.min_notional


# --- F-2 validate_rules (A-114 ③, DP-54 형식: params, LOT_SIZE stepSize, minNotional) ----------------

def test_start_rule_check_accepts_matching_step() -> None:
    """F-2 수정: 시작 규칙 검사(D-209)가 이 전략의 validate_rules를 부른다. 고정 LOT_STEP = 거래소 stepSize이면 문제 0."""
    for name in ("btc-trend-v1.toml", "btc-trend-v1-paper.toml"):
        assert BtcTrendV1.validate_rules(params(name), RULES.lot_step, RULES.min_notional) == []
        si = [StrategyInput(BtcTrendV1(), params(name), EVAL)]
        assert spot_rule_problems(si, {SYM: RULES}, PAIRS) == []


@pytest.mark.parametrize("step", ["0.0001", "0.000001", "0.00000001", "1"])
def test_start_rule_check_rejects_mismatched_step(step: str) -> None:
    """거래소 stepSize가 전략 고정 LOT_STEP과 다르면 시작 거부(주문 0) — 더 굵은 단위(거래소 거부)·더 가는 단위(조용한 차이) 모두."""
    p = params("btc-trend-v1-paper.toml")
    [problem] = BtcTrendV1.validate_rules(p, D(step), RULES.min_notional)
    assert "LOT_STEP" in problem and step in problem
    si = [StrategyInput(BtcTrendV1(), p, EVAL)]
    out = spot_rule_problems(si, {SYM: replace(RULES, lot_step=D(step))}, PAIRS)
    assert len(out) == 1 and SYM in out[0] and "LOT_STEP" in out[0]


def test_start_rule_check_refuses_symbol_without_rules() -> None:
    """규칙이 적재되지 않은 심볼은 불일치로 본다(불확실하면 시작하지 않음) — validate_rules가 있어 이제 이 경로를 탄다."""
    si = [StrategyInput(BtcTrendV1(), params("btc-trend-v1-paper.toml"), EVAL)]
    out = spot_rule_problems(si, {}, PAIRS)
    assert len(out) == 1 and "거래 규칙 없음" in out[0]


# --- F-3 validate_deploy (A-114 ③): 하루 1회 판정·M2 전용 ---------------------------------------------

def test_deploy_check_accepts_single_daily_eval() -> None:
    s = BtcTrendV1()
    for name in ("btc-trend-v1.toml", "btc-trend-v1-paper.toml"):
        assert s.validate_deploy(params(name), EVAL) == []
        assert s.validate_deploy(params(name), (time(1, 0),), market="M2") == []


@pytest.mark.parametrize("times", [(), (time(0, 15), time(12, 0)), (time(0, 15), time(0, 15)),
                                   (time(0, 15), time(8, 0), time(16, 0))])
def test_deploy_check_requires_exactly_one_daily_eval(times: tuple[time, ...]) -> None:
    [problem] = BtcTrendV1().validate_deploy(params("btc-trend-v1-paper.toml"), times)
    assert "정확히 1개" in problem and f"{len(times)}개" in problem


@pytest.mark.parametrize("market", ["M1", "M3", "", "m2"])
def test_deploy_check_refuses_market_other_than_m2(market: str) -> None:
    [problem] = BtcTrendV1().validate_deploy(params("btc-trend-v1-paper.toml"), EVAL, market=market)
    assert "M2" in problem


def test_deploy_check_refuses_spec_whose_markets_are_not_m2_only() -> None:
    """명세 markets가 M2만이 아니면(예: 복사·수정 중 M1 추가) 판정 시각과 무관하게 거부."""
    s = BtcTrendV1()
    for markets in (("M1",), ("M1", "M2"), ()):
        s.spec = replace(s.spec, markets=markets)
        problems = s.validate_deploy(params("btc-trend-v1-paper.toml"), EVAL)
        assert len(problems) == 1 and "markets" in problems[0], problems


def test_deploy_check_reports_all_problems_together() -> None:
    problems = BtcTrendV1().validate_deploy(params("btc-trend-v1-paper.toml"), (time(0, 15), time(12, 0)), market="M1")
    assert len(problems) == 2


# --- 규칙 동작 (가상 봉) ------------------------------------------------------------------------------

def flat_bars(n: int = 60, close: Decimal = D("50000")) -> list[Bar]:
    start = date(2026, 8, 1)
    return [Bar(start + timedelta(days=i), close, close + 500, close - 500, close, D("10")) for i in range(n)]


def breakout_bars(last_close: Decimal) -> list[Bar]:
    """평평한 59봉(TR 1000, 종가 = last_close × 0.98) 뒤 마지막 봉이 돌파."""
    base = (last_close * D("0.98")).quantize(D("0.01"))
    bars = flat_bars(59, base)
    return [*bars, Bar(bars[-1].at + timedelta(days=1), base, last_close + 500, base - 500, last_close, D("10"))]


def snapshot(bars: list[Bar], *, last: Decimal, position: PositionView | None = None) -> EvaluationSnapshot:
    view = InstrumentView(SYM, daily_bars=tuple(bars), last_price=last, position=position)
    return EvaluationSnapshot("ev", "M2", "BN-PAPER-01", AT, D("10000"), (view,), next_evaluation_at=AT + timedelta(days=1))


def test_entry_on_breakout_with_floored_quantity_and_stop() -> None:
    bars = flat_bars(59)
    bars.append(Bar(bars[-1].at + timedelta(days=1), D("50000"), D("51500"), D("50500"), D("51000"), D("10")))
    atr = D(13 * 1000 + 1500) / 14                     # TR: 1000 × 13, 마지막 |51500 − 50000| = 1500
    last = D("51000")
    [s] = BtcTrendV1().evaluate(snapshot(bars, last=last), params("btc-trend-v1-paper.toml"))
    assert (s.rule_id, s.kind, s.order_type, s.price) == ("ENTRY-1", RequestType.ENTRY, OrderType.LIMIT, last)
    assert s.quantity == D("0.00185")                  # min(40/(2·ATR), 1000/51000, 95/(51000·1.006)=0.0018516…) 내림
    assert s.stop_price == last - D("2") * atr
    [v] = BtcTrendV1().evaluate(snapshot(bars, last=last), params("btc-trend-v1.toml"))
    assert v.quantity == D("0.19215")                  # 0.98 × 10000 / 51000 = 0.192156… (위험 기준 0.2413…보다 작음)
    assert s.valid_until == AT + timedelta(minutes=30)


def test_no_entry_when_close_only_equals_prior_high() -> None:
    assert BtcTrendV1().evaluate(snapshot(flat_bars(), last=D("50000")), params("btc-trend-v1-paper.toml")) == []


def test_exit1_below_sma_and_exit2_takes_priority() -> None:
    bars = flat_bars(59)
    bars.append(Bar(bars[-1].at + timedelta(days=1), D("50000"), D("50400"), D("49400"), D("49900"), D("10")))
    mine = PositionView(D("0.002"), D("50000"), 10, True)
    [x] = BtcTrendV1().evaluate(snapshot(bars, last=D("49900"), position=mine), params("btc-trend-v1-paper.toml"))
    assert (x.rule_id, x.kind, x.order_type, x.quantity) == ("EXIT-1", RequestType.EXIT, OrderType.MARKET, D("0.002"))
    high_cost = PositionView(D("0.002"), D("60000"), 10, True)   # 60000 − 2·ATR(≈1000) > 49900 → 손절
    [y] = BtcTrendV1().evaluate(snapshot(bars, last=D("49900"), position=high_cost), params("btc-trend-v1-paper.toml"))
    assert y.rule_id == "EXIT-2"


def test_manual_holding_blocks_entry_and_is_never_exited() -> None:
    manual = PositionView(D("1"), D("0"), 0, False)
    p = params("btc-trend-v1-paper.toml")
    assert BtcTrendV1().evaluate(snapshot(breakout_bars(D("60000")), last=D("60000"), position=manual), p) == []
    bars = flat_bars(59)
    bars.append(Bar(bars[-1].at + timedelta(days=1), D("50000"), D("50400"), D("49400"), D("49900"), D("10")))
    assert BtcTrendV1().evaluate(snapshot(bars, last=D("49900"), position=manual), p) == []
