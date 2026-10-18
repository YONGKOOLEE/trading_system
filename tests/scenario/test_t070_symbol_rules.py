"""CP-041 T-070 — 종목별 시작 규칙 검사 (F-3, D-270·D-271·D-280 — 승인 A-122, 착수 A-123 ③).

검증: V-340(L1·L2 — 새 형식 정상·뒤바뀐 규칙·규칙 없음), V-341(L1 — 기존 형식 1심볼 호환·2심볼 거부·두 형식·형식 없음),
V-342(L1·L2 — 설정 유니버스 2심볼에 m2-verify만: BTCUSDT만 검사, ETH 신호 0), D-270 백테스트 CLI 두 곳 같은 검사·
TESTNET_RULES ETHUSDT(BN §15), D-280 M1 시작은 이 검사를 부르지 않음.
네트워크 없음. 규칙값은 테스트넷 공개 규칙 실측값(docs/broker-specs/binance-spot.md §12 BTCUSDT 2026-10-14, §15 ETHUSDT
2026-10-18) — 가격·잔고 아님. 가격은 가상값.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import UTC, date, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t020_main import make_root
from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.m2_engine_world import FixedM2Strategy, SymbolRulesCheckedStrategy, fixed_spec
from trading_system.app import backtest_m2_cli as cli
from trading_system.app import main as app_main
from trading_system.app import wiring
from trading_system.app.backtest_m2_cli import TESTNET_RULES, backtest_m2_main, backtest_m2_minute_main
from trading_system.app.main import run_main
from trading_system.app.wiring import StrategyInput, spot_rule_problems
from trading_system.config.strategy_params import load_params_file
from trading_system.domain.enums import Mode, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView
from trading_system.domain.trading_rules import SymbolRules
from trading_system.storage.repository import OrderIdentity, Repository
from trading_system.strategy.btc_trend import BtcTrendV1
from trading_system.strategy.crypto_trend import CryptoTrendV3
from trading_system.strategy.m2_verify import M2Verify

ROOT = Path(__file__).resolve().parents[2]
D = Decimal
BTC, ETH = "BTCUSDT", "ETHUSDT"
PAIRS2 = {BTC: ("BTC", "USDT"), ETH: ("ETH", "USDT")}
LEGACY_NEEDED = "종목별 규칙 검사 불가 — validate_symbol_rules 필요 (CP-041 D-271)"

# 테스트넷 BTCUSDT 실측값 (BN §12, 2026-10-14) — 운영 표와 별개로 시험에 고정
BTC_RULES = SymbolRules(
    instrument=BTC, tick_size=D("0.01"), min_price=D("0.01"), max_price=D("1000000"),
    lot_min_qty=D("0.00001"), lot_max_qty=D("9000"), lot_step=D("0.00001"),
    market_min_qty=D("0"), market_max_qty=D("133.97193096"), market_step=D("0"),
    min_notional=D("5"), max_notional=D("9000000"), apply_min_to_market=True, apply_max_to_market=False)
# 테스트넷 ETHUSDT 실측값 (BN §15, 2026-10-18). §15에 기록이 없는 MARKET_LOT maxQty·maxNotional·applyMaxToMarket은
# 비활성(0·None·False)으로 둔다 — 운영 TESTNET_RULES와 같은 처리 [미확인 항목, 시작 검사에는 쓰이지 않음]
ETH_RULES = SymbolRules(
    instrument=ETH, tick_size=D("0.01"), min_price=D("0.01"), max_price=D("1000000"),
    lot_min_qty=D("0.0001"), lot_max_qty=D("9000"), lot_step=D("0.0001"),
    market_min_qty=D("0"), market_max_qty=D("0"), market_step=D("0"),
    min_notional=D("5"), max_notional=None, apply_min_to_market=True, apply_max_to_market=False)
RULES2 = {BTC: BTC_RULES, ETH: ETH_RULES}
NEW_PARAMS = {"qty": D("0.001"), "notional": D("10")}


class Legacy2(FixedM2Strategy):
    """기존 형식만 가진 2심볼 전략(crypto-trend v2 형태). validate_rules 호출을 기록한다."""

    calls: list[tuple[Decimal, Decimal]] = []

    def __init__(self) -> None:
        super().__init__(fixed_spec(strategy_id="t-legacy2", universe=(BTC, ETH),
                                    capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05")))

    @staticmethod
    def validate_rules(params: dict, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        Legacy2.calls.append((lot_step, min_notional))
        return []


class BothForms(FixedM2Strategy):
    """두 형식을 모두 가진 2심볼 전략 — 새 형식만 불려야 한다(D-271 (2)). 기존 형식이 불리면 시험 실패."""

    seen: list[tuple[str, str]] = []

    def __init__(self) -> None:
        super().__init__(fixed_spec(strategy_id="t-both", universe=(BTC, ETH)))

    @staticmethod
    def validate_symbol_rules(params: dict, symbol: str, rules: SymbolRules) -> list[str]:
        BothForms.seen.append((symbol, rules.instrument))
        return [f"새 형식 {rules.lot_step}"] if symbol == BTC else []

    @staticmethod
    def validate_rules(params: dict, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        raise AssertionError("두 형식을 가진 전략에 기존 형식이 불렸다 (D-271 (2) 위반)")


@pytest.fixture(autouse=True)
def _clear_records() -> None:
    Legacy2.calls.clear()
    BothForms.seen.clear()


def new_si() -> list[StrategyInput]:
    return [StrategyInput(SymbolRulesCheckedStrategy(), NEW_PARAMS, ())]


def m2v_params() -> dict:
    return load_params_file(M2Verify().spec, ROOT / "config" / "strategies" / "m2-verify-v1.toml")


# --- V-340 새 형식 (L1) ------------------------------------------------------------------------------

def test_v340_measured_rules_pass_for_both_symbols() -> None:
    assert spot_rule_problems(new_si(), RULES2, PAIRS2) == []


def test_v340_each_symbol_gets_its_own_rules() -> None:
    """시스템이 심볼을 돌며 그 심볼과 그 심볼의 규칙을 넘긴다(D-270)."""
    assert spot_rule_problems([StrategyInput(BothForms(), {}, ())], RULES2, PAIRS2) == ["t-both:1 BTCUSDT: 새 형식 0.00001"]
    assert sorted(BothForms.seen) == [(BTC, BTC), (ETH, ETH)]


def test_v340_swapped_lot_steps_one_problem_per_symbol_with_symbol_first() -> None:
    """BTC에 ETH 단위, ETH에 BTC 단위 → 심볼 이름이 앞에 든 문제 각 1건 (F-3의 경우)."""
    rules = {BTC: replace(BTC_RULES, lot_step=D("0.0001")), ETH: replace(ETH_RULES, lot_step=D("0.00001"))}
    btc, eth = spot_rule_problems(new_si(), rules, PAIRS2)
    assert btc.startswith("t-m2symrules:1 BTCUSDT: ") and "0.0001" in btc
    assert eth.startswith("t-m2symrules:1 ETHUSDT: ") and "0.00001" in eth


def test_v340_rules_of_other_symbol_refused() -> None:
    """BTC 자리에 ETH 규칙, ETH 자리에 BTC 규칙(규칙 통째 뒤바뀜) → 두 심볼 모두 거부, 문제마다 그 심볼이 앞에."""
    problems = spot_rule_problems(new_si(), {BTC: ETH_RULES, ETH: BTC_RULES}, PAIRS2)
    assert any(p.startswith("t-m2symrules:1 BTCUSDT: ") for p in problems)
    assert any(p.startswith("t-m2symrules:1 ETHUSDT: ") for p in problems)
    assert all(p.startswith(("t-m2symrules:1 BTCUSDT: ", "t-m2symrules:1 ETHUSDT: ")) for p in problems)


def test_v340_missing_rules_for_one_symbol_refused() -> None:
    assert spot_rule_problems(new_si(), {BTC: BTC_RULES}, PAIRS2) == ["t-m2symrules:1 ETHUSDT: 거래 규칙 없음"]


def test_v340_crypto_trend_v3_other_symbols_step_refused() -> None:
    """F-3 해소: crypto-trend v3(두 형식)의 BTCUSDT에 ETH 단위 0.0001 → 거부 (기존 형식은 같은 경우를 통과)."""
    p = load_params_file(CryptoTrendV3().spec, ROOT / "config" / "strategies" / "crypto-trend-v3-paper.toml")
    si = [StrategyInput(CryptoTrendV3(), p, ())]
    assert spot_rule_problems(si, RULES2, PAIRS2) == []
    [problem] = spot_rule_problems(si, {**RULES2, BTC: replace(BTC_RULES, lot_step=D("0.0001"))}, PAIRS2)
    assert problem.startswith("crypto-trend:3 BTCUSDT: ") and "LOT_SIZE stepSize 0.0001" in problem
    assert CryptoTrendV3.validate_rules(p, D("0.0001"), D("5")) == []   # 비교: 기존 형식만이면 통과했을 경우


# --- V-341 기존 형식 호환 (L1) -------------------------------------------------------------------------

@pytest.mark.parametrize("pairs", [{BTC: ("BTC", "USDT")}, PAIRS2])
def test_v341_m2_verify_single_symbol_legacy_same_result_and_wording(pairs: dict) -> None:
    """m2-verify(기존 형식, 명세 유니버스 BTCUSDT): 설정 유니버스가 1심볼이든 2심볼이든 범위 = BTCUSDT 1개 → 기존 형식 그대로."""
    si = [StrategyInput(M2Verify(), m2v_params(), ())]
    assert spot_rule_problems(si, RULES2, pairs) == []
    bad = replace(BTC_RULES, lot_step=D("0.0001"), min_notional=D("6"))
    expected = [f"m2-verify:1 BTCUSDT: {x}" for x in M2Verify.validate_rules(m2v_params(), D("0.0001"), D("6"))]
    assert len(expected) == 2 and spot_rule_problems(si, {**RULES2, BTC: bad}, pairs) == expected
    assert spot_rule_problems(si, {ETH: ETH_RULES}, pairs) == ["m2-verify:1 BTCUSDT: 거래 규칙 없음"]


def test_v341_btc_trend_single_symbol_legacy_same_result() -> None:
    p = load_params_file(BtcTrendV1().spec, ROOT / "config" / "strategies" / "btc-trend-v1-paper.toml")
    si = [StrategyInput(BtcTrendV1(), p, ())]
    assert spot_rule_problems(si, RULES2, PAIRS2) == []
    expected = [f"btc-trend:1 BTCUSDT: {x}" for x in BtcTrendV1.validate_rules(p, D("0.0001"), D("5"))]
    assert expected and spot_rule_problems(si, {**RULES2, BTC: replace(BTC_RULES, lot_step=D("0.0001"))},
                                           PAIRS2) == expected


def test_v341_legacy_two_symbols_refused_without_calling_legacy() -> None:
    """기존 형식만 + 범위 2심볼 → 시작 거부 사유 1건(규칙이 맞아도), 기존 형식은 부르지 않는다."""
    si = [StrategyInput(Legacy2(), {"qty": D("0.001")}, ())]
    assert spot_rule_problems(si, RULES2, PAIRS2) == [f"t-legacy2:1 BTCUSDT, ETHUSDT: {LEGACY_NEEDED}"]
    assert Legacy2.calls == []


def test_v341_legacy_two_symbol_spec_but_one_symbol_in_scope_uses_legacy() -> None:
    """범위 = 명세 유니버스 ∩ 설정 유니버스(D-271 (1)). 설정이 1심볼이면 범위 1개 → 기존 형식 그대로 호출(D-271 (3))."""
    si = [StrategyInput(Legacy2(), {"qty": D("0.001")}, ())]
    assert spot_rule_problems(si, RULES2, {BTC: ("BTC", "USDT")}) == []
    assert Legacy2.calls == [(D("0.00001"), D("5"))]


def test_v341_no_check_method_means_no_check() -> None:
    """둘 다 없으면 현행대로 검사 없음 — 규칙이 없어도 이 함수는 문제를 내지 않는다(규칙 적재 검사는 호출자 몫)."""
    si = [StrategyInput(FixedM2Strategy(fixed_spec(universe=(BTC, ETH))), {"qty": D("1")}, ())]
    assert spot_rule_problems(si, {}, PAIRS2) == []


# --- V-342 일부 심볼 전략 범위 (L1) ---------------------------------------------------------------------

def test_v342_m2_verify_checked_only_for_btc_when_universe_has_eth(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[Decimal] = []
    original = M2Verify.validate_rules

    def spy(params: dict, lot_step: Decimal, min_notional: Decimal) -> list[str]:
        calls.append(lot_step)
        return original(params, lot_step, min_notional)

    monkeypatch.setattr(M2Verify, "validate_rules", staticmethod(spy))
    assert spot_rule_problems([StrategyInput(M2Verify(), m2v_params(), ())], RULES2, PAIRS2) == []
    assert calls == [D("0.00001")]   # BTCUSDT 규칙만 — ETH 단위 0.0001과 대조 0


def test_v342_m2_verify_gives_no_eth_signal() -> None:
    at = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)
    bar = (Bar(date(2026, 10, 19), *(D("59000"),) * 4, D("1")),)
    snap = EvaluationSnapshot("ev", "M2", "BN-BT-01", at, D("10000"), (
        InstrumentView(instrument=BTC, daily_bars=bar, last_price=D("60000")),
        InstrumentView(instrument=ETH, daily_bars=bar, last_price=D("2000"))))
    signals = M2Verify().evaluate(snap, m2v_params())
    assert [(s.instrument, s.kind) for s in signals] == [(BTC, RequestType.ENTRY)]


# --- L2: run --market M2 (BACKTEST, fake) 시작 순서 — 규칙 불일치는 DB 전 거부, 주문 0 --------------------------

UNIVERSE2 = 'quote_asset = "USDT"\nsymbols = ["BTCUSDT", "ETHUSDT"]\n'


def _root(tmp: Path, module: str, strategy_id: str, params: str, eval_times: str = '["01:00"]') -> Path:
    root = make_m2_root(tmp, Mode.BACKTEST, symbols=UNIVERSE2)
    (root / "docs").mkdir()
    shutil.copy(ROOT / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    (root / "config" / "strategies").mkdir()
    (root / "config" / "strategies" / f"{strategy_id}.toml").write_text(
        f'strategy = "{strategy_id}"\nversion = 1\n[params]\n{params}', encoding="utf-8")
    app = root / "config" / "m2" / "app.backtest.toml"
    app.write_text(app.read_text(encoding="utf-8") + f'\n[[strategies]]\nmodule = "{module}"\n'
                   f'params = "config/strategies/{strategy_id}.toml"\neval_times = {eval_times}\n', encoding="utf-8")
    return root


def _new_root(tmp: Path) -> Path:
    return _root(tmp, "tests.support.m2_engine_world:SymbolRulesCheckedStrategy", "t-m2symrules",
                 'qty = "0.001"\nnotional = "10"\n')


def _intents(db: Path) -> int:
    repo = Repository(db, Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        return int(repo.select("SELECT COUNT(*) AS n FROM intent")[0]["n"])
    finally:
        repo.close()


def _rules_patch(monkeypatch: pytest.MonkeyPatch, rules: dict[str, SymbolRules]) -> None:
    monkeypatch.setattr(app_main, "_m2_exchange_rules", lambda mode, symbols: (dict(rules), None))


def test_v340_l2_measured_rules_start_and_no_orders(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    root = _new_root(tmp_path)
    _rules_patch(monkeypatch, RULES2)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "규칙 불일치" not in out and "시작: 시장 M2" in out
    assert _intents(root / "var" / "backtest" / "trading.db") == 0


def test_v340_l2_swapped_steps_refuse_start_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                  capsys: pytest.CaptureFixture[str]) -> None:
    root = _new_root(tmp_path)
    _rules_patch(monkeypatch, {BTC: replace(BTC_RULES, lot_step=D("0.0001")),
                               ETH: replace(ETH_RULES, lot_step=D("0.00001"))})
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부(주문 없음)" in out and "규칙 불일치" in out
    assert "t-m2symrules:1 BTCUSDT: " in out and "t-m2symrules:1 ETHUSDT: " in out
    assert not (root / "var" / "backtest" / "trading.db").exists()


def test_v340_l2_missing_symbol_rules_refuse_start_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    root = _new_root(tmp_path)
    _rules_patch(monkeypatch, {BTC: BTC_RULES})
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부(주문 없음)" in out and "ETHUSDT" in out
    assert not (root / "var" / "backtest" / "trading.db").exists()


def test_v341_l2_legacy_two_symbols_refuse_start_no_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    root = _root(tmp_path, "tests.scenario.test_t070_symbol_rules:Legacy2", "t-legacy2", 'qty = "0.001"\n')
    _rules_patch(monkeypatch, RULES2)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    out = capsys.readouterr().out
    assert "규칙 불일치" in out and LEGACY_NEEDED in out
    assert not (root / "var" / "backtest" / "trading.db").exists()


def test_v342_l2_universe_btc_eth_m2_verify_only_starts(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                        capsys: pytest.CaptureFixture[str]) -> None:
    """설정 유니버스 BTCUSDT·ETHUSDT, 배포 m2-verify만, 규칙 두 심볼 → 시작 통과(A-119 비고 위험 없음), 주문 0."""
    src = (ROOT / "config" / "strategies" / "m2-verify-v1.toml").read_text(encoding="utf-8")
    params = src.split("[params]", 1)[1].lstrip("\n")
    root = _root(tmp_path, "trading_system.strategy.m2_verify:M2Verify", "m2-verify", params,
                 eval_times='["01:00", "04:00"]')
    _rules_patch(monkeypatch, RULES2)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 0, capsys.readouterr().out
    out = capsys.readouterr().out
    assert "규칙 불일치" not in out and "시작: 시장 M2" in out
    assert _intents(root / "var" / "backtest" / "trading.db") == 0


# --- D-270 백테스트 CLI 두 곳: 같은 검사, 거래 규칙 표 ETHUSDT (BN §15) ----------------------------------------

def test_testnet_rules_table_has_ethusdt_from_bn15() -> None:
    assert TESTNET_RULES[BTC] == BTC_RULES
    assert TESTNET_RULES[ETH] == ETH_RULES
    assert cli.PAIRS == PAIRS2


def test_daily_cli_refuses_legacy_two_symbol_strategy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """일봉 CLI: 범위 = 전략 명세 유니버스 ∩ 거래 규칙 표(설정 유니버스 역할) → 기존 형식 2심볼 거부(D-271 (5)), 데이터 읽기 전."""
    params = tmp_path / "t-legacy2.toml"
    params.write_text('strategy = "t-legacy2"\nversion = 1\n[params]\nqty = "0.001"\n', encoding="utf-8")
    code = backtest_m2_main(["--strategy", "tests.scenario.test_t070_symbol_rules:Legacy2", "--params", str(params),
                             "--data", str(tmp_path / "none.csv"), "--end", "2025-03-04", "--run-id", "t",
                             "--slippage", "0.001", "--eval-time", "01:00", "--out", str(tmp_path / "out")], ROOT)
    out = capsys.readouterr().out
    assert code == 2 and "거래 규칙 검사" in out and LEGACY_NEEDED in out
    assert Legacy2.calls == [] and not (tmp_path / "out").exists()


def test_minute_cli_uses_symbol_rules_for_crypto_trend(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    """1분봉 CLI: crypto-trend v3에 BTCUSDT 규칙 단위가 ETH 단위면 거부(F-3) — 실운영과 같은 함수."""
    monkeypatch.setattr(cli, "TESTNET_RULES", {**TESTNET_RULES, BTC: replace(BTC_RULES, lot_step=D("0.0001"))})
    code = backtest_m2_minute_main(
        ["--strategy", "trading_system.strategy.crypto_trend:CryptoTrendV3",
         "--params", "config/strategies/crypto-trend-v3-paper.toml", "--data", str(tmp_path / "none.csv"),
         "--start", "2025-03-03", "--end", "2025-03-04", "--run-id", "t", "--lookback", "60",
         "--slippage", "0.001", "--out", str(tmp_path / "out")], ROOT)
    out = capsys.readouterr().out
    assert code == 2 and "거래 규칙 검사" in out and "crypto-trend:3 BTCUSDT: " in out
    assert not (tmp_path / "out").exists()


# --- D-280 M1 불변: M1 시작은 이 검사를 부르지 않는다 ------------------------------------------------------

def test_d280_m1_run_does_not_call_spot_rule_problems(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    def boom(*a: object, **k: object) -> list[str]:
        raise AssertionError("M1 시작이 M2 규칙 검사를 불렀다")

    monkeypatch.setattr(wiring, "spot_rule_problems", boom)
    root = make_root(tmp_path, Mode.BACKTEST)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.2) == 0
