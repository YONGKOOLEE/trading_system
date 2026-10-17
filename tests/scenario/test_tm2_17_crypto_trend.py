"""T-M2-17 후보 crypto-trend v1 (전략실 → 운영 복사본, 미등록·미승인 — A-118 A, docs/strategies/crypto-trend/review-v1.md).

검증 범위(구조·적재·등록 가능 여부만 — 재현·성과 판정은 장중 엔진 T-068 뒤):
- 파라미터 파일 2개(config/strategies/crypto-trend-v1.toml, -paper.toml)가 명세로 적재·검증되는지.
- 명세 능력: CAP-TRG-02·CAP-DAT-01·CAP-ORD-01·CAP-INS-05만(CAP-INS-01·CAP-DAT-02·CAP-HLD-02 없음). 코드가 실제로 읽는
  입력이 확정 일봉(daily_bars)·판정 시세(last_price)·상태 입력뿐인지 — AST와 동작으로 확인.
- 등록(docs/capabilities.md M2 열, 이 브랜치): BACKTEST 통과(CAP-TRG-02 "검증 중"), PAPER 거부(능력 4개 "검증 중").
- M2 조립 검사(이 브랜치 T-052 D-219): 유니버스 밖 ETHUSDT 거부(A-096), 주기 격자 검사 거부(파라미터 period_minutes 없음),
  validate_deploy(판정 시각 23개 전제)와 D-219(CAP-TRG-02 전략 eval_times = []) 충돌 — 결함 F-1·F-2는 고치지 않고 xfail strict로
  기록, validate_rules 종목 구분 없음은 F-3 xfail strict.
- 재현 가능 여부: 1분봉 실행기(T-053)가 이 전략을 거부하고, 일봉 입력을 주지 않는다(daily_bars ()) → 신호 0.
- 규칙 동작 최소 확인(가상 봉): 돌파 + 게이트 진입·수량 내림·손절가, 게이트 차단, EXIT-1, EXIT-2 우선, 수동 보유 무시, 두 종목 독립.
가격·잔고는 가상값이다. 네트워크 없음. 전략 로직은 전략실 원본(08ebd6f)과 바이트 단위로 같다.
"""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.app.backtest_m2_cli import PAIRS, TESTNET_RULES, backtest_m2_minute_main
from trading_system.app.wiring import StrategyInput, _spot_strategy_problems, period_grid_of, spot_rule_problems
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_params import load_params_file
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.strategy import crypto_trend
from trading_system.strategy.crypto_trend import CryptoTrendV1

ROOT = Path(__file__).resolve().parents[2]
D = Decimal
BTC, ETH = "BTCUSDT", "ETHUSDT"
CAPS = load_capabilities(ROOT / "docs" / "capabilities.md")
RULES = TESTNET_RULES[BTC]                         # 테스트넷 실측 (2026-10-14): LOT step 0.00001, minNotional 5
HOURLY = tuple(time(h, 0) for h in range(1, 24))   # 전략실 validate_deploy 전제: 60분 격자 01:00~23:00 23개 (D-258)
AT = datetime(2026, 10, 20, 5, 0, 2, tzinfo=UTC)


def params(name: str) -> dict[str, object]:
    return load_params_file(CryptoTrendV1().spec, ROOT / "config" / "strategies" / name)


def m2_paper_limits():  # type: ignore[no-untyped-def]
    return load_limits_file(ROOT / "config" / "m2" / "limits.paper.toml", market="M2").limits


# --- 파라미터 파일 ------------------------------------------------------------------------------------

def test_both_param_files_load_with_lab_values() -> None:
    v1, paper = params("crypto-trend-v1.toml"), params("crypto-trend-v1-paper.toml")
    rules_part = {"entry_n": 25, "exit_n": 50, "stop_atr": D("2")}
    assert {k: v1[k] for k in rules_part} == rules_part == {k: paper[k] for k in rules_part}
    assert (v1["risk_ratio"], v1["max_weight"], v1["order_cap"]) == (D("0.025"), D("0.49"), D("1000000000"))
    assert (paper["risk_ratio"], paper["max_weight"], paper["order_cap"]) == (D("0.002"), D("0.05"), D("95"))
    assert "period_minutes" not in v1 and "period_minutes" not in paper   # F-1 (아래)


# --- 명세·능력 ----------------------------------------------------------------------------------------

def test_spec_rules_priorities_and_capabilities() -> None:
    spec = CryptoTrendV1().spec
    assert (spec.strategy_id, spec.version, spec.markets, spec.universe) == ("crypto-trend", 1, ("M2",), (BTC, ETH))
    assert [(r.rule_id, r.kind, r.priority) for r in spec.rules] == [("EXIT-2", "EXIT", 1), ("EXIT-1", "EXIT", 2),
                                                                     ("ENTRY-1", "ENTRY", 1)]
    assert all(r.trigger == "CAP-TRG-02" and r.inputs == ("CAP-DAT-01", "상태 입력") for r in spec.rules)
    assert set(spec.capabilities) == {"CAP-TRG-02", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-05"}
    for absent in ("CAP-INS-01", "CAP-TRG-01", "CAP-DAT-02", "CAP-HLD-02"):
        assert absent not in spec.capabilities
    assert spec.validity_max_minutes == 30 and spec.sizing == "S-3" and spec.universe_method == "U-A"


def test_code_reads_only_daily_bars_last_price_and_state_inputs() -> None:
    """선언(CAP-DAT-01만, CAP-DAT-02 없음)과 코드가 맞는지: evaluate가 종목 입력에서 읽는 속성을 AST로 모은다."""
    tree = ast.parse((ROOT / "src" / "trading_system" / "strategy" / "crypto_trend.py").read_text(encoding="utf-8"))
    attrs = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute) and isinstance(n.value, ast.Name)
             and n.value.id == "v"}
    assert attrs == {"instrument", "data_ok", "has_pending_order", "daily_bars", "last_price", "position", "statuses"}
    assert "minute_bars" not in attrs and "minute_ok" not in attrs


def test_minute_bars_do_not_change_signals_and_daily_bars_are_required() -> None:
    p = params("crypto-trend-v1-paper.toml")
    bars, last = breakout_bars(D("60000")), D("60001")   # 판정 시세가 마지막 확정 종가(60000)를 넘음
    base = CryptoTrendV1().evaluate(snapshot([view(BTC, bars, last)]), p)
    junk = tuple(Bar(datetime(2026, 10, 20, 4, m, tzinfo=UTC), D("1"), D("1"), D("1"), D("1"), D("1")) for m in range(60))
    assert CryptoTrendV1().evaluate(snapshot([replace(view(BTC, bars, last), minute_bars=junk)]), p) == base != []
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, [], last)]), p) == []          # 일봉 없음 → 신호 없음
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, bars[:49], last)]), p) == []   # 확정 일봉 < 50 → 무효


# --- 등록 (capabilities.md M2 열, 이 브랜치) ---------------------------------------------------------

def test_registration_backtest_passes_with_trg02_verifying_and_paper_refused() -> None:
    spec = CryptoTrendV1().spec
    assert validate_spec(spec, CAPS, m2_paper_limits(), Mode.BACKTEST) == []
    refused = validate_spec(spec, CAPS, m2_paper_limits(), Mode.PAPER)
    assert sorted(p.split(":")[0] for p in refused) == ["CAP-DAT-01", "CAP-INS-05", "CAP-ORD-01", "CAP-TRG-02"]
    assert all("검증 중" in p and "PAPER 등록 불가" in p for p in refused), refused
    # 전략실이 CAP-INS-05로 선언한 것(btc-trend F-1 반영) — 국내 상장주식 능력이면 BACKTEST부터 거부된다
    wrong = replace(spec, capabilities=("CAP-TRG-02", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"))
    assert any("CAP-INS-01" in p for p in validate_spec(wrong, CAPS, m2_paper_limits(), Mode.BACKTEST))


# --- M2 조립 검사 (현재 동작 기록) --------------------------------------------------------------------

def test_m2_assembly_refuses_ethusdt_outside_universe_a096() -> None:
    """운영 M2 유니버스(A-096, config/m2/universe.paper.toml)는 BTCUSDT만 → ETHUSDT는 조립 단계 거부(사용자 결정 B 대기)."""
    si = [StrategyInput(CryptoTrendV1(), params("crypto-trend-v1-paper.toml"), ())]
    [problem] = _spot_strategy_problems(si, PAIRS)
    assert "유니버스 밖 심볼 ETHUSDT" in problem
    assert ETH not in TESTNET_RULES   # ETH 거래 규칙 실측 없음 (STEPS의 0.0001은 전략실 가정)


def test_period_grid_check_refuses_missing_period_minutes_now() -> None:
    """현재 동작: D-219 주기 격자 검사(실운영 조립·1분봉 실행기 공통)는 정수 파라미터 period_minutes를 요구 → 거부."""
    p = params("crypto-trend-v1-paper.toml")
    with pytest.raises(ConfigError, match="period_minutes이 필요"):
        period_grid_of(StrategyInput(CryptoTrendV1(), p, ()), "M2")
    with pytest.raises(ConfigError, match="eval_times는 빈 목록"):
        period_grid_of(StrategyInput(CryptoTrendV1(), p, HOURLY), "M2")


def test_validate_deploy_requires_23_times_now() -> None:
    p = params("crypto-trend-v1-paper.toml")
    s = CryptoTrendV1()
    assert s.validate_deploy(p, HOURLY) == [] and s.validate_deploy(p, HOURLY, market="M2") == []
    [problem] = s.validate_deploy(p, ())
    assert "23개" in problem and "0개" in problem
    assert len(s.validate_deploy(p, HOURLY, market="M1")) == 1


@pytest.mark.xfail(strict=True, reason="F-1: 파라미터 period_minutes 없음 — D-219·D-258(A-115 Q-1) 주기 격자 검사 거부")
def test_f1_period_grid_gives_60min_grid_of_23_decisions() -> None:
    grid = period_grid_of(StrategyInput(CryptoTrendV1(), params("crypto-trend-v1-paper.toml"), ()), "M2")
    assert grid is not None and grid.period_minutes == 60 and len(grid.day_times(date(2026, 10, 20))) == 23


@pytest.mark.xfail(strict=True, reason="F-2: validate_deploy가 판정 시각 23개를 요구 — D-219 CAP-TRG-02 배포는 eval_times []")
def test_f2_validate_deploy_accepts_d219_periodic_deploy() -> None:
    assert CryptoTrendV1().validate_deploy(params("crypto-trend-v1-paper.toml"), ()) == []


@pytest.mark.xfail(strict=True, reason="F-3: validate_rules 서명에 종목이 없어 BTCUSDT에 ETH 단위 0.0001이 와도 통과")
def test_f3_start_rule_check_refuses_other_symbols_step() -> None:
    p = params("crypto-trend-v1-paper.toml")
    si = [StrategyInput(CryptoTrendV1(), p, ())]
    assert spot_rule_problems(si, {BTC: replace(RULES, lot_step=D("0.0001"))}, PAIRS) != []


def test_start_rule_check_current_behaviour() -> None:
    """현재 validate_rules: 어느 종목의 고정 단위와도 다르면 거부, 같으면(종목 무관) 통과. 실측 BTC 단위는 통과."""
    p = params("crypto-trend-v1-paper.toml")
    assert crypto_trend.STEPS[BTC] == RULES.lot_step == D("0.00001")
    assert spot_rule_problems([StrategyInput(CryptoTrendV1(), p, ())], {BTC: RULES}, PAIRS) == []
    [problem] = CryptoTrendV1.validate_rules(p, D("0.001"), RULES.min_notional)
    assert "0.001" in problem
    assert CryptoTrendV1.validate_rules(p, D("0.0001"), RULES.min_notional) == []   # F-3 (종목 구분 없음)


# --- 재현 가능 여부: 1분봉 실행기 (T-053) ------------------------------------------------------------

def test_minute_backtest_cli_refuses_this_strategy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """1분봉 실행기는 실운영과 같은 주기 격자 검사를 먼저 한다 → F-1로 데이터 읽기 전에 거부(종료 코드 2)."""
    code = backtest_m2_minute_main(
        ["--strategy", "trading_system.strategy.crypto_trend:CryptoTrendV1",
         "--params", "config/strategies/crypto-trend-v1-paper.toml", "--data", str(tmp_path / "none.csv"),
         "--start", "2020-01-01", "--end", "2020-01-31", "--run-id", "t", "--lookback", "60",
         "--slippage", "0.001", "--out", str(tmp_path)], ROOT)
    assert code == 2
    assert "period_minutes" in capsys.readouterr().out
    assert not any(tmp_path.iterdir())


def test_minute_engine_input_shape_gives_no_signal() -> None:
    """1분봉 실행기의 판정 입력 모양(daily_bars (), 최근가 = 직전 닫힌 1분봉 종가, 단일 심볼)으로는 이 전략이 신호를 낼 수
    없다 — F-1·F-2를 고쳐도 일봉 입력·두 심볼이 없으면 재현이 아니다(T-068 범위)."""
    p = params("crypto-trend-v1-paper.toml")
    minute = tuple(Bar(datetime(2026, 10, 20, 4, m, tzinfo=UTC), D("60000"), D("60100"), D("59900"), D("60050"), D("1"))
                   for m in range(60))
    v = InstrumentView(BTC, minute_bars=minute, last_price=D("60050"))
    assert CryptoTrendV1().evaluate(snapshot([v]), p) == []


# --- 규칙 동작 (가상 봉) ------------------------------------------------------------------------------

def flat_bars(n: int = 60, close: Decimal = D("50000"), spread: Decimal = D("500")) -> list[Bar]:
    start = date(2026, 8, 1)
    return [Bar(start + timedelta(days=i), close, close + spread, close - spread, close, D("10")) for i in range(n)]


def breakout_bars(last_close: Decimal, spread: Decimal = D("500")) -> list[Bar]:
    """평평한 59봉(종가 = last_close × 0.98) 뒤 마지막 봉 종가 last_close (마지막 확정 봉)."""
    base = (last_close * D("0.98")).quantize(D("0.01"))
    bars = flat_bars(59, base, spread)
    return [*bars, Bar(bars[-1].at + timedelta(days=1), base, last_close + spread, base - spread, last_close, D("10"))]


def view(sym: str, bars: list[Bar], last: Decimal, position: PositionView | None = None) -> InstrumentView:
    return InstrumentView(sym, daily_bars=tuple(bars), last_price=last, position=position)


def snapshot(views: list[InstrumentView]) -> EvaluationSnapshot:
    return EvaluationSnapshot("ev", "M2", "BN-PAPER-01", AT, D("10000"), tuple(views),
                              next_evaluation_at=AT + timedelta(hours=1))


def test_entry_needs_price_above_prior_high_and_sma_gate() -> None:
    bars = flat_bars(59)
    bars.append(Bar(bars[-1].at + timedelta(days=1), D("50000"), D("50500"), D("49500"), D("50000"), D("10")))
    p = params("crypto-trend-v1-paper.toml")
    atr = D("1000")                                         # TR 모두 1000
    [s] = CryptoTrendV1().evaluate(snapshot([view(BTC, bars, D("50100"))]), p)   # 판정 시세가 확정 종가 최고 50000 위
    assert (s.rule_id, s.kind, s.order_type, s.price) == ("ENTRY-1", RequestType.ENTRY, OrderType.LIMIT, D("50100"))
    assert s.quantity == D("0.00188")      # min(20/2000=0.01, 500/50100=0.00998, 95/(50100×1.006)=0.0018848…) 내림
    assert s.stop_price == D("50100") - 2 * atr and s.valid_until == AT + timedelta(minutes=30)
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, bars, D("50000"))]), p) == []   # 같은 값은 돌파 아님


def test_gate_blocks_breakout_below_sma() -> None:
    """25일 최고는 넘지만 SMA(50) 아래 → 진입 없음(게이트 — 진입 즉시 청산 상태 금지)."""
    bars = flat_bars(35, D("60000")) + [
        Bar(date(2026, 9, 5) + timedelta(days=i), D("50000"), D("50500"), D("49500"), D("50000"), D("10"))
        for i in range(25)]
    p = params("crypto-trend-v1-paper.toml")
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, bars, D("51000"))]), p) == []    # SMA50 = 55000
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, bars, D("56000"))]), p) != []    # 둘 다 넘으면 진입


def test_exit1_below_sma_and_exit2_takes_priority() -> None:
    bars = flat_bars(60)
    p = params("crypto-trend-v1-paper.toml")
    mine = PositionView(D("0.002"), D("50000"), 3, True)
    [x] = CryptoTrendV1().evaluate(snapshot([view(BTC, bars, D("49900"), mine)]), p)
    assert (x.rule_id, x.kind, x.order_type, x.quantity) == ("EXIT-1", RequestType.EXIT, OrderType.MARKET, D("0.002"))
    high_cost = PositionView(D("0.002"), D("60000"), 3, True)   # 60000 − 2·1000 > 49900 → 손절
    [y] = CryptoTrendV1().evaluate(snapshot([view(BTC, bars, D("49900"), high_cost)]), p)
    assert y.rule_id == "EXIT-2"
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, bars, D("50100"), mine)]), p) == []   # SMA 위 보유 유지


def test_manual_holding_blocks_entry_and_is_never_exited() -> None:
    manual = PositionView(D("1"), D("0"), 0, False)
    p = params("crypto-trend-v1-paper.toml")
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, breakout_bars(D("60000")), D("60001"), manual)]), p) == []
    assert CryptoTrendV1().evaluate(snapshot([view(BTC, flat_bars(60), D("49000"), manual)]), p) == []


def test_two_symbols_independent_with_own_lot_steps() -> None:
    p = params("crypto-trend-v1-paper.toml")
    eth_bars = breakout_bars(D("3000"), D("30"))
    sigs = CryptoTrendV1().evaluate(snapshot([view(BTC, flat_bars(60), D("49000")),
                                              view(ETH, eth_bars, D("3001"))]), p)
    [s] = sigs
    assert s.instrument == ETH and s.rule_id == "ENTRY-1"
    assert s.quantity == D("0.0314")       # 95/(3001×1.006)=0.03146… → ETH 단위 0.0001(전략실 가정)로 내림
    assert CryptoTrendV1().evaluate(snapshot([view("XRPUSDT", eth_bars, D("3001"))]), p) == []   # 명세 밖 종목 무시


@pytest.mark.parametrize("price", ["15000.00", "60000.00", "126000.00", "999999.99"])
def test_paper_size_stays_inside_l7_and_above_min_notional(price: str) -> None:
    """PAPER 축소판(order_cap 95): IOC 지정가(시세 × 1.005) 기준 주문 금액 ≤ L7 100 USDT(A-064), 내림 뒤 ≥ minNotional 5."""
    pr = D(price)
    [s] = CryptoTrendV1().evaluate(snapshot([view(BTC, breakout_bars(pr), pr + 1)]), params("crypto-trend-v1-paper.toml"))
    assert s.quantity * s.price * D("1.005") <= m2_paper_limits().l7_max_order_amount
    assert s.quantity * s.price >= RULES.min_notional
