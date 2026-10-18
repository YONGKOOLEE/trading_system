"""T-M2-17 후보 crypto-trend v3 (전략실 → 운영 복사본, 미등록·미승인 — A-118 A·A-119 ①·A-123 ①,
docs/strategies/crypto-trend/review-v3.md).

v3(전략실 작업 트리 strategy.py sha256 f6f23ef7…, 2026-10-18)는 v2에 CP-041 D-270 종목별 시작 규칙 검사
validate_symbol_rules(params, symbol, rules: SymbolRules)만 더했다 — 판정·수량·신호·파라미터 값은 v2와 같다(diff 확인, 아래 규칙
동작 시험 그대로 통과). 기존 validate_rules는 T-070 전 호환용으로 남았다(D-271 (2)).
v2(전략실 커밋 a5c1405)는 운영 v1 검토(9fdb517)의 F-1·F-2를 반영했다 — 파라미터 period_minutes(= 60) 추가, validate_deploy는
eval_times 빈 목록·period_minutes 60·M2를 요구. v1 시험 이력은 9fdb517, v2 시험 이력은 b8fb28c.

검증 범위(구조·적재·등록 가능 여부만 — 재현·성과 판정은 장중 엔진 T-068 확장 뒤):
- validate_symbol_rules(D-270): 실측 규칙 통과(BTCUSDT 테스트넷 §12, ETHUSDT §15 값), 거부 4종(STEPS 밖 심볼·규칙 심볼 이름 다름·
  LOT_SIZE 단위 다름·MARKET_LOT_SIZE 단위 배수 아님), MARKET step 0·배수 통과. 운영 시작 검사(app.wiring.spot_rule_problems)는
  CP-041 T-070 뒤 validate_symbol_rules만 부른다(D-271 (2)) — F-3 시험은 xfail을 떼고 정상 시험이 됐다(T-070 완료 기준).
- 파라미터 파일 2개(config/strategies/crypto-trend-v3.toml, -paper.toml)가 명세로 적재·검증되는지(period_minutes 60 포함).
- 명세 능력: CAP-TRG-02·CAP-DAT-01·CAP-ORD-01·CAP-INS-05만(CAP-INS-01·CAP-DAT-02·CAP-HLD-02 없음). 코드가 실제로 읽는
  입력이 확정 일봉(daily_bars)·판정 시세(last_price)·상태 입력뿐인지 — AST와 동작으로 확인.
- 등록(docs/capabilities.md M2 열, 이 브랜치): BACKTEST 통과(CAP-TRG-02 "검증 중"), PAPER 거부(능력 4개 "검증 중").
- M2 조립 검사(이 브랜치 T-052 D-219): 주기 격자 통과(60분 23회, 실운영 등록 경로 _register_strategies 포함), validate_deploy
  ([]·60·M2 통과 / 23개·15분·M1 거부), 유니버스 밖 ETHUSDT 거부(A-096 운영 유니버스 — CP-041 Q-5 (다) 반영은 나중), 기존
  validate_rules 종목 구분 없음은 전략 메서드 자체의 기록(운영 시작 검사는 T-070 뒤 새 형식만 부른다).
- 재현 가능 여부: 1분봉 실행기(T-053)는 이제 이 전략을 받아 끝까지 돌지만 일봉 입력을 주지 않아(daily_bars ()) 신호 0 — 재현 아님.
- 규칙 동작 최소 확인(가상 봉): 돌파 + 게이트 진입·수량 내림·손절가, 게이트 차단, EXIT-1, EXIT-2 우선, 수동 보유 무시, 두 종목 독립.
가격·잔고는 가상값이다. 네트워크 없음. 파라미터 파일은 전략실 원본과 바이트 단위로 같고, 전략 코드는 CRLF → LF 변환(D-063)만
다르다(줄 내용 동일).
"""

from __future__ import annotations

import ast
import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t053_m2_minute_backtest import row as mrow
from tests.scenario.test_t053_m2_minute_backtest import write_csv
from tests.support.m2_fakes import ACCOUNT, build_m2_env
from trading_system.app.backtest_m2_cli import PAIRS, TESTNET_RULES, backtest_m2_minute_main
from trading_system.app.wiring import (
    StrategyInput, _register_strategies, _spot_strategy_problems, period_grid_of, spot_rule_problems,
)
from trading_system.config.capabilities import load_capabilities
from trading_system.config.errors import ConfigError
from trading_system.config.limits_file import load_limits_file
from trading_system.config.m2_files import load_universe
from trading_system.config.strategy_params import load_params_file
from trading_system.config.strategy_registry import StrategyRegistry, validate_spec
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.strategy import crypto_trend
from trading_system.strategy.crypto_trend import CryptoTrendV3

ROOT = Path(__file__).resolve().parents[2]
D = Decimal
BTC, ETH = "BTCUSDT", "ETHUSDT"
CAPS = load_capabilities(ROOT / "docs" / "capabilities.md")
RULES = TESTNET_RULES[BTC]                         # 테스트넷 실측 (2026-10-14): LOT step 0.00001, minNotional 5
HOURLY = tuple(time(h, 0) for h in range(1, 24))   # v1 validate_deploy 전제(23개) — v2·D-219에서는 거부되는 배포 표현
AT = datetime(2026, 10, 20, 5, 0, 2, tzinfo=UTC)


def params(name: str) -> dict[str, object]:
    return load_params_file(CryptoTrendV3().spec, ROOT / "config" / "strategies" / name)


def m2_paper_limits():  # type: ignore[no-untyped-def]
    return load_limits_file(ROOT / "config" / "m2" / "limits.paper.toml", market="M2").limits


# --- 파라미터 파일 ------------------------------------------------------------------------------------

def test_both_param_files_load_with_lab_values() -> None:
    full, paper = params("crypto-trend-v3.toml"), params("crypto-trend-v3-paper.toml")
    rules_part = {"entry_n": 25, "exit_n": 50, "stop_atr": D("2"), "period_minutes": 60}   # v2: period_minutes (F-1 해소)
    assert {k: full[k] for k in rules_part} == rules_part == {k: paper[k] for k in rules_part}
    assert (full["risk_ratio"], full["max_weight"], full["order_cap"]) == (D("0.025"), D("0.49"), D("1000000000"))
    assert (paper["risk_ratio"], paper["max_weight"], paper["order_cap"]) == (D("0.002"), D("0.05"), D("95"))
    assert type(paper["period_minutes"]) is int and set(full) == set(paper) and len(full) == 7


# --- 명세·능력 ----------------------------------------------------------------------------------------

def test_spec_rules_priorities_and_capabilities() -> None:
    spec = CryptoTrendV3().spec
    assert (spec.strategy_id, spec.version, spec.markets, spec.universe) == ("crypto-trend", 3, ("M2",), (BTC, ETH))
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
    p = params("crypto-trend-v3-paper.toml")
    bars, last = breakout_bars(D("60000")), D("60001")   # 판정 시세가 마지막 확정 종가(60000)를 넘음
    base = CryptoTrendV3().evaluate(snapshot([view(BTC, bars, last)]), p)
    junk = tuple(Bar(datetime(2026, 10, 20, 4, m, tzinfo=UTC), D("1"), D("1"), D("1"), D("1"), D("1")) for m in range(60))
    assert CryptoTrendV3().evaluate(snapshot([replace(view(BTC, bars, last), minute_bars=junk)]), p) == base != []
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, [], last)]), p) == []          # 일봉 없음 → 신호 없음
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, bars[:49], last)]), p) == []   # 확정 일봉 < 50 → 무효


# --- 등록 (capabilities.md M2 열, 이 브랜치) ---------------------------------------------------------

def test_registration_backtest_passes_with_trg02_verifying_and_paper_refused() -> None:
    spec = CryptoTrendV3().spec
    assert validate_spec(spec, CAPS, m2_paper_limits(), Mode.BACKTEST) == []
    refused = validate_spec(spec, CAPS, m2_paper_limits(), Mode.PAPER)
    assert sorted(p.split(":")[0] for p in refused) == ["CAP-DAT-01", "CAP-INS-05", "CAP-ORD-01", "CAP-TRG-02"]
    assert all("검증 중" in p and "PAPER 등록 불가" in p for p in refused), refused
    # 전략실이 CAP-INS-05로 선언한 것(btc-trend F-1 반영) — 국내 상장주식 능력이면 BACKTEST부터 거부된다
    wrong = replace(spec, capabilities=("CAP-TRG-02", "CAP-DAT-01", "CAP-ORD-01", "CAP-INS-01"))
    assert any("CAP-INS-01" in p for p in validate_spec(wrong, CAPS, m2_paper_limits(), Mode.BACKTEST))


# --- M2 조립 검사 ----------------------------------------------------------------------------------------

def test_m2_assembly_refuses_ethusdt_outside_universe_a096() -> None:
    """운영 M2 유니버스(A-096, config/m2/universe.paper.toml)는 BTCUSDT만 → ETHUSDT는 조립 단계 거부.
    ETHUSDT 유니버스 반영은 CP-041 D-272 순서(T-070·T-071 뒤, Q-5 (다) — 나중). [T-070 개정] 이전에는 백테스트 CLI의 PAIRS를
    운영 유니버스 대용으로 썼으나, T-070(D-270)이 그 표에 ETHUSDT(BN §15)를 넣었으므로 설정 파일을 직접 읽는다."""
    universe = load_universe(ROOT / "config" / "m2" / "universe.paper.toml")
    assert universe.symbols == (BTC,)
    pairs = {s: (universe.base_asset(s), universe.quote_asset) for s in universe.symbols}
    si = [StrategyInput(CryptoTrendV3(), params("crypto-trend-v3-paper.toml"), ())]
    [problem] = _spot_strategy_problems(si, pairs)
    assert "유니버스 밖 심볼 ETHUSDT" in problem
    assert ETH in TESTNET_RULES   # 백테스트 거래 규칙 표에는 T-070에서 ETHUSDT 추가(운영 유니버스와 별개)


@pytest.mark.parametrize("name", ["crypto-trend-v3.toml", "crypto-trend-v3-paper.toml"])
def test_f1_resolved_period_grid_gives_60min_grid_of_23_decisions(name: str) -> None:
    """F-1 해소(v2): D-219·D-258(A-115 Q-1) 주기 격자 검사 통과 → 60분 격자, 01:00~23:00 하루 23회(00:00 판정 없음)."""
    grid = period_grid_of(StrategyInput(CryptoTrendV3(), params(name), ()), "M2")
    assert grid is not None and grid.period_minutes == 60
    times = grid.day_times(date(2026, 10, 20))
    assert len(times) == 23 and times[0].hour == 1 and times[-1].hour == 23


def test_period_grid_still_refuses_eval_times_and_m1() -> None:
    p = params("crypto-trend-v3-paper.toml")
    with pytest.raises(ConfigError, match="eval_times는 빈 목록"):
        period_grid_of(StrategyInput(CryptoTrendV3(), p, HOURLY), "M2")
    with pytest.raises(ConfigError, match="M1 프로파일에서 열지 않는다"):
        period_grid_of(StrategyInput(CryptoTrendV3(), p, ()), "M1")


def test_operational_registration_path_builds_60min_grid_in_backtest(tmp_path: Path) -> None:
    """실운영 조립의 등록 경로(app.wiring._register_strategies — 등록부 검사 → 파라미터 검증·형 변환 → period_grid_of)를
    M2·BACKTEST 저장소로 통과하고 LoadedStrategy에 60분 격자가 실린다. PAPER 저장소·등록이면 같은 경로가 거부한다."""
    si = StrategyInput(CryptoTrendV3(), params("crypto-trend-v3-paper.toml"), ())
    env = build_m2_env(tmp_path / "bt", mode=Mode.BACKTEST)
    try:
        reg = StrategyRegistry(env.repo, env.clock, env.halts, market="M2")
        [loaded] = _register_strategies(reg, [si], CAPS, m2_paper_limits(), Mode.BACKTEST, ACCOUNT, market="M2")
        assert loaded.period is not None and loaded.period.period_minutes == 60 and loaded.eval_times == ()
        assert loaded.params["period_minutes"] == 60
    finally:
        env.repo.close()
    env = build_m2_env(tmp_path / "paper", mode=Mode.PAPER)
    try:
        reg = StrategyRegistry(env.repo, env.clock, env.halts, market="M2")
        with pytest.raises(ConfigError) as e:
            _register_strategies(reg, [si], CAPS, m2_paper_limits(), Mode.PAPER, ACCOUNT, market="M2")
        assert e.value.problems and all("전략 등록 거부" in x and "검증 중" in x for x in e.value.problems)
    finally:
        env.repo.close()


def test_f2_resolved_validate_deploy_accepts_d219_periodic_deploy() -> None:
    """F-2 해소(v2): eval_times [] + period_minutes 60 + M2만 통과. 판정 시각 23개·15분·M1·period_minutes 없음은 거부."""
    p = params("crypto-trend-v3-paper.toml")
    s = CryptoTrendV3()
    assert s.validate_deploy(p, ()) == [] and s.validate_deploy(p, (), market="M2") == []
    assert s.validate_deploy(params("crypto-trend-v3.toml"), (), market="M2") == []
    [times] = s.validate_deploy(p, HOURLY)
    assert "빈 목록" in times and "23개" in times
    [fifteen] = s.validate_deploy({**p, "period_minutes": 15}, ())
    assert "period_minutes는 60" in fifteen
    [m1] = s.validate_deploy(p, (), market="M1")
    assert "M2 전용" in m1
    assert len(s.validate_deploy({k: v for k, v in p.items() if k != "period_minutes"}, ())) == 1
    assert len(s.validate_deploy({**p, "period_minutes": True}, ())) == 1


def test_period_15_passes_grid_but_validate_deploy_refuses() -> None:
    """D-258은 M2 등록 period_minutes ∈ {15, 60}이고 명세 범위도 15~60 — 15분은 격자 검사를 통과하지만 이 전략의
    validate_deploy가 막는다(전략실 근거는 60분, 15분은 열위). 1분봉 실행기도 validate_deploy를 부른다."""
    p = {**params("crypto-trend-v3-paper.toml"), "period_minutes": 15}
    grid = period_grid_of(StrategyInput(CryptoTrendV3(), p, ()), "M2")
    assert grid is not None and grid.period_minutes == 15
    assert CryptoTrendV3().validate_deploy(p, ()) != []


def test_f3_start_rule_check_refuses_other_symbols_step() -> None:
    """F-3 해소(CP-041 T-070, D-270·D-271 (2)): 운영 시작 검사가 validate_symbol_rules를 심볼마다 불러 BTCUSDT에 ETH 단위
    0.0001이 오면 거부한다. [T-070 개정] xfail(strict) 제거 — T-070 완료로 통과해야 하는 시험. 단언은 그대로(!= [])."""
    p = params("crypto-trend-v3-paper.toml")
    si = [StrategyInput(CryptoTrendV3(), p, ())]
    probs = spot_rule_problems(si, {BTC: replace(RULES, lot_step=D("0.0001"))}, {BTC: PAIRS[BTC]})   # 범위 = BTC 하나
    assert len(probs) == 1 and probs[0].startswith("crypto-trend:3 BTCUSDT: ") and "0.0001" in probs[0], probs
    # (재검토 2026-10-18: PAIRS가 BTC·ETH라 ETH '거래 규칙 없음'만으로도 != []가 되어 BTC 단위 검사를 확인하지 못했다)


def test_start_rule_check_current_behaviour() -> None:
    """기존 validate_rules(전략 메서드): 어느 종목의 고정 단위와도 다르면 거부, 같으면(종목 무관) 통과. 실측 규칙은 시작 검사 통과.
    [T-070 개정] 시작 검사 입력을 거래 규칙 표 전체로 바꿈 — 표·PAIRS에 ETHUSDT가 들어가 범위가 두 심볼이 됐다."""
    p = params("crypto-trend-v3-paper.toml")
    assert crypto_trend.STEPS[BTC] == RULES.lot_step == D("0.00001")
    assert spot_rule_problems([StrategyInput(CryptoTrendV3(), p, ())], TESTNET_RULES, PAIRS) == []
    [problem] = CryptoTrendV3.validate_rules(p, D("0.001"), RULES.min_notional)
    assert "0.001" in problem
    assert CryptoTrendV3.validate_rules(p, D("0.0001"), RULES.min_notional) == []   # F-3 (종목 구분 없음)


# --- v3 종목별 시작 규칙 검사 validate_symbol_rules (CP-041 D-270·D-271, A-122) ----------------------------

# ETHUSDT 테스트넷 규칙 (main 655b47d binance-spot.md §15, 2026-10-18): LOT step 0.0001·minQty 0.0001·maxQty 9000,
# MARKET_LOT minQty 0·stepSize 0, tick 0.01, minNotional 5·applyMinToMarket True. 이 시험은 TESTNET_RULES에 ETH가 없던
# 때(T-070 전) 만든 값이다 — 운영 표 TESTNET_RULES[ETH](T-070)와의 일치는 test_t070_symbol_rules가 본다. MARKET maxQty·maxNotional은
# §15에 기록이 없어 BTCUSDT 값을 빌린 가상값(검사에 쓰이지 않음).
ETH_RULES = replace(RULES, instrument=ETH, lot_min_qty=D("0.0001"), lot_max_qty=D("9000"), lot_step=D("0.0001"),
                    market_min_qty=D("0"), market_step=D("0"), tick_size=D("0.01"), min_notional=D("5"),
                    apply_min_to_market=True)


def test_v3_has_new_and_legacy_rule_checks() -> None:
    """D-271 (2): 두 형식을 모두 가진 전략 — 운영은 T-070 뒤 새 형식만 부른다. 둘 다 정적 메서드, 반환은 문자열 목록."""
    assert isinstance(CryptoTrendV3.__dict__["validate_symbol_rules"], staticmethod)
    assert isinstance(CryptoTrendV3.__dict__["validate_rules"], staticmethod)
    p = params("crypto-trend-v3-paper.toml")
    assert CryptoTrendV3().validate_symbol_rules(p, BTC, RULES) == []   # 인스턴스로 불러도 같음


@pytest.mark.parametrize("name", ["crypto-trend-v3.toml", "crypto-trend-v3-paper.toml"])
def test_symbol_rules_pass_with_measured_testnet_rules(name: str) -> None:
    """V-340 정상: 테스트넷 실측 규칙(BTC §12 = TESTNET_RULES, ETH §15)이면 두 심볼 모두 문제 0. params는 쓰지 않는다."""
    p = params(name)
    assert ETH_RULES.instrument == ETH and ETH_RULES.lot_step == crypto_trend.STEPS[ETH]
    assert CryptoTrendV3.validate_symbol_rules(p, BTC, RULES) == []
    assert CryptoTrendV3.validate_symbol_rules(p, ETH, ETH_RULES) == []
    assert CryptoTrendV3.validate_symbol_rules({}, BTC, RULES) == []


def test_symbol_rules_refuse_swapped_lot_steps_with_symbol_name() -> None:
    """V-340 거부: BTC에 ETH 단위, ETH에 BTC 단위 → 심볼 이름이 든 문제 각 1건. F-3의 바로 그 경우(기존 validate_rules는 통과)."""
    p = params("crypto-trend-v3-paper.toml")
    [btc] = CryptoTrendV3.validate_symbol_rules(p, BTC, replace(RULES, lot_step=D("0.0001")))
    assert btc.startswith(f"{BTC}:") and "LOT_SIZE stepSize 0.0001" in btc and "0.00001" in btc
    [eth] = CryptoTrendV3.validate_symbol_rules(p, ETH, replace(ETH_RULES, lot_step=D("0.00001")))
    assert eth.startswith(f"{ETH}:") and "LOT_SIZE stepSize 0.00001" in eth
    assert CryptoTrendV3.validate_rules(p, D("0.0001"), RULES.min_notional) == []   # 비교: 기존 형식은 같은 경우를 통과


def test_symbol_rules_refuse_symbol_outside_steps() -> None:
    """거부: 전략 수량 단위 표(STEPS) 밖 심볼 → 문제 1건(나머지 검사 생략). 규칙 내용이 맞아 보여도 거부."""
    [problem] = CryptoTrendV3.validate_symbol_rules({}, "SOLUSDT", replace(RULES, instrument="SOLUSDT"))
    assert problem.startswith("SOLUSDT:") and "STEPS" in problem and BTC in problem and ETH in problem


def test_symbol_rules_refuse_rules_for_other_instrument() -> None:
    """거부: 넘겨받은 규칙의 심볼 이름이 다름(호출부가 규칙을 잘못 짝지은 경우). 단위가 같아도 거부, 단위까지 다르면 2건."""
    [name_only] = CryptoTrendV3.validate_symbol_rules({}, BTC, replace(RULES, instrument="BTCUSDC"))
    assert name_only.startswith(f"{BTC}:") and "'BTCUSDC'" in name_only
    problems = CryptoTrendV3.validate_symbol_rules({}, BTC, ETH_RULES)
    assert len(problems) == 2 and all(x.startswith(f"{BTC}:") for x in problems)


@pytest.mark.parametrize(("sym", "market_step", "ok"), [
    (BTC, "0", True),            # 0 = LOT_SIZE stepSize와 같음(D-208) → 검사 생략
    (BTC, "0.00001", True),      # 같은 단위
    (BTC, "0.000001", True),     # 전략 단위가 MARKET 단위의 배수
    (BTC, "0.00002", False),     # 0.00001 % 0.00002 ≠ 0
    (BTC, "0.001", False),
    (ETH, "0", True),
    (ETH, "0.00005", True),
    (ETH, "0.0003", False),
])
def test_symbol_rules_market_step_multiple(sym: str, market_step: str, ok: bool) -> None:
    """거부: MARKET_LOT_SIZE stepSize(> 0)의 배수가 아닌 전략 단위 — 청산은 시장가로 매수 수량 그대로 내므로."""
    rules = replace(RULES if sym == BTC else ETH_RULES, market_step=D(market_step))
    problems = CryptoTrendV3.validate_symbol_rules({}, sym, rules)
    if ok:
        assert problems == []
    else:
        [problem] = problems
        assert problem.startswith(f"{sym}:") and "MARKET_LOT_SIZE" in problem and "배수가 아님" in problem


def test_operational_start_check_uses_symbol_form_after_t070() -> None:
    """운영 시작 검사(app.wiring.spot_rule_problems)는 T-070 뒤 두 형식을 가진 전략에 validate_symbol_rules만 부른다(D-271 (2)).
    [T-070 개정] 이전 시험(…still_uses_legacy_form_until_t070)은 T-070 전 동작(통과)의 기록이었다 — 요구사항(CP-041 A-122)대로
    거부로 바꾼다. 영향 ID: D-270·D-271·V-341."""
    si = [StrategyInput(CryptoTrendV3(), params("crypto-trend-v3-paper.toml"), ())]
    bad = replace(RULES, lot_step=D("0.0001"))
    assert CryptoTrendV3.validate_symbol_rules({}, BTC, bad) != []
    [problem] = spot_rule_problems(si, {**TESTNET_RULES, BTC: bad}, PAIRS)
    assert problem.startswith(f"crypto-trend:3 {BTC}: ")


# --- 재현 가능 여부: 1분봉 실행기 (T-053) ------------------------------------------------------------

def test_minute_backtest_cli_now_accepts_but_gives_no_signal(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """v3(v2와 같음)는 1분봉 실행기의 실운영 같은 일정 검사(주기 격자·validate_deploy·거래 규칙)를 통과해 끝까지 돈다. 그러나 이
    실행기는 일봉 입력을 주지 않으므로(daily_bars ()) 가격이 계속 올라도 신호 0 — 재현이 아니다(T-068 확장 범위, A-119 ②).
    가상 1분봉 2일(매분 +0.01 상승)."""
    first = datetime(2025, 3, 3, tzinfo=UTC)
    rows = []
    for i in range(1440 * 2):
        c = str(D("60000.00") + D("0.01") * i)
        rows.append(mrow(first + timedelta(minutes=i), o=c, h=c, lo=c, c=c))
    data = write_csv(tmp_path / "BTCUSDT-1m.csv", rows)
    code = backtest_m2_minute_main(
        ["--strategy", "trading_system.strategy.crypto_trend:CryptoTrendV3",
         "--params", "config/strategies/crypto-trend-v3-paper.toml", "--data", str(data),
         "--start", "2025-03-03", "--end", "2025-03-04", "--run-id", "t", "--lookback", "60",
         "--slippage", "0.001", "--out", str(tmp_path / "out")], ROOT)
    assert code == 0, capsys.readouterr().out
    s = json.loads((tmp_path / "out" / "t" / "summary.json").read_text(encoding="utf-8"))
    assert (s["version"], s["period_minutes"], s["decisions"]) == (3, 60, 2 * 23)
    assert s["signals"] == 0 and s["round_trips"] == 0 and s["fills"] == {"BUY": 0, "SELL": 0}


def test_minute_engine_input_shape_gives_no_signal() -> None:
    """1분봉 실행기의 판정 입력 모양(daily_bars (), 최근가 = 직전 닫힌 1분봉 종가, 단일 심볼)으로는 이 전략이 신호를 낼 수
    없다 — F-1·F-2가 해소돼도 일봉 입력·두 심볼이 없으면 재현이 아니다(T-068 범위 확장, A-119 ②)."""
    p = params("crypto-trend-v3-paper.toml")
    minute = tuple(Bar(datetime(2026, 10, 20, 4, m, tzinfo=UTC), D("60000"), D("60100"), D("59900"), D("60050"), D("1"))
                   for m in range(60))
    v = InstrumentView(BTC, minute_bars=minute, last_price=D("60050"))
    assert CryptoTrendV3().evaluate(snapshot([v]), p) == []


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
    p = params("crypto-trend-v3-paper.toml")
    atr = D("1000")                                         # TR 모두 1000
    [s] = CryptoTrendV3().evaluate(snapshot([view(BTC, bars, D("50100"))]), p)   # 판정 시세가 확정 종가 최고 50000 위
    assert (s.rule_id, s.kind, s.order_type, s.price) == ("ENTRY-1", RequestType.ENTRY, OrderType.LIMIT, D("50100"))
    assert s.quantity == D("0.00188")      # min(20/2000=0.01, 500/50100=0.00998, 95/(50100×1.006)=0.0018848…) 내림
    assert s.stop_price == D("50100") - 2 * atr and s.valid_until == AT + timedelta(minutes=30)
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, bars, D("50000"))]), p) == []   # 같은 값은 돌파 아님


def test_gate_blocks_breakout_below_sma() -> None:
    """25일 최고는 넘지만 SMA(50) 아래 → 진입 없음(게이트 — 진입 즉시 청산 상태 금지)."""
    bars = flat_bars(35, D("60000")) + [
        Bar(date(2026, 9, 5) + timedelta(days=i), D("50000"), D("50500"), D("49500"), D("50000"), D("10"))
        for i in range(25)]
    p = params("crypto-trend-v3-paper.toml")
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, bars, D("51000"))]), p) == []    # SMA50 = 55000
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, bars, D("56000"))]), p) != []    # 둘 다 넘으면 진입


def test_exit1_below_sma_and_exit2_takes_priority() -> None:
    bars = flat_bars(60)
    p = params("crypto-trend-v3-paper.toml")
    mine = PositionView(D("0.002"), D("50000"), 3, True)
    [x] = CryptoTrendV3().evaluate(snapshot([view(BTC, bars, D("49900"), mine)]), p)
    assert (x.rule_id, x.kind, x.order_type, x.quantity) == ("EXIT-1", RequestType.EXIT, OrderType.MARKET, D("0.002"))
    high_cost = PositionView(D("0.002"), D("60000"), 3, True)   # 60000 − 2·1000 > 49900 → 손절
    [y] = CryptoTrendV3().evaluate(snapshot([view(BTC, bars, D("49900"), high_cost)]), p)
    assert y.rule_id == "EXIT-2"
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, bars, D("50100"), mine)]), p) == []   # SMA 위 보유 유지


def test_manual_holding_blocks_entry_and_is_never_exited() -> None:
    manual = PositionView(D("1"), D("0"), 0, False)
    p = params("crypto-trend-v3-paper.toml")
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, breakout_bars(D("60000")), D("60001"), manual)]), p) == []
    assert CryptoTrendV3().evaluate(snapshot([view(BTC, flat_bars(60), D("49000"), manual)]), p) == []


def test_two_symbols_independent_with_own_lot_steps() -> None:
    p = params("crypto-trend-v3-paper.toml")
    eth_bars = breakout_bars(D("3000"), D("30"))
    sigs = CryptoTrendV3().evaluate(snapshot([view(BTC, flat_bars(60), D("49000")),
                                              view(ETH, eth_bars, D("3001"))]), p)
    [s] = sigs
    assert s.instrument == ETH and s.rule_id == "ENTRY-1"
    assert s.quantity == D("0.0314")       # 95/(3001×1.006)=0.03146… → ETH 단위 0.0001(main 655b47d binance-spot §15 테스트넷 확인값)로 내림
    assert CryptoTrendV3().evaluate(snapshot([view("XRPUSDT", eth_bars, D("3001"))]), p) == []   # 명세 밖 종목 무시


@pytest.mark.parametrize("price", ["15000.00", "60000.00", "126000.00", "999999.99"])
def test_paper_size_stays_inside_l7_and_above_min_notional(price: str) -> None:
    """PAPER 축소판(order_cap 95): IOC 지정가(시세 × 1.005) 기준 주문 금액 ≤ L7 100 USDT(A-064), 내림 뒤 ≥ minNotional 5."""
    pr = D(price)
    [s] = CryptoTrendV3().evaluate(snapshot([view(BTC, breakout_bars(pr), pr + 1)]), params("crypto-trend-v3-paper.toml"))
    assert s.quantity * s.price * D("1.005") <= m2_paper_limits().l7_max_order_amount
    assert s.quantity * s.price >= RULES.min_notional
