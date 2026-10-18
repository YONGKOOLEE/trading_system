"""T-M2-15 M2 PAPER-0 검증 전략 `m2-verify` v1 (확정안, 미승인 — docs/strategies/m2-verify/strategy.md).

새 M2 실행 경로(CP-036, A-106) 기준 검증:
- 전략 규칙(split_time UTC 전 진입만·이후 청산만, 수동 보유 무시), 수량 내림(S-1 notional ÷ 최근가, qty_step 내림),
  배포 검사(validate_deploy), 거래소 규칙 검사(validate_rules — DP-54 형식, 테스트넷 실측값), 파라미터 파일.
- 등록: 실제 docs/capabilities.md(M2 열 "검증 중", CAP-INS-05) 기준 BACKTEST 등록 통과, PAPER는 M2 PAPER-0 검증 예외
  (D-212, 승인 A-ID)로만, `scripts/strategy.py register --market M2` 경로(V-289 방식).
- 시작: `run --market M2`(BACKTEST fake) — 실측 형태 규칙이면 시작(K8), 규칙 불일치면 시작 거부(D-209).
- 하루 왕복: build_engine(market="M2")(실운영 조립과 같은 함수, D-202) + fake 현물 브로커 — UTC 01:00 IOC 지정가 매수 →
  수수료 = BTC → 순 수령 → UTC 04:00 시장가 매도(LOT_SIZE 내림, D-208) → dust → 소유권 해제 → 다음 날 재진입.
  IOC 체결 0, 테스트넷 지급 BTC 1개(A-106 Q-1 (가): 기준선 = 수동 보유 → 운영 요청 /exit → 그 뒤 전략 진입).
거래 규칙 값은 테스트넷 실측(broker-specs/binance-spot.md §12, 2026-10-14). **가격·잔고·수수료율은 가상값**이다. 네트워크 없음.
"""

from __future__ import annotations

import shutil
from collections.abc import Iterator
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.m2_engine_world import ACCOUNT, M2World
from trading_system.app import main as app_main
from trading_system.app.main import run_main
from trading_system.app.strategy_tool import strategy_main
from trading_system.app.wiring import StrategyInput, spot_rule_problems
from trading_system.config.capabilities import load_capabilities
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_params import load_params_file
from trading_system.config.strategy_registry import PAPER0_M2_CAPABILITIES, validate_spec
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.market import TradingStatus
from trading_system.domain.models import Signal
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView, evaluation_id_of
from trading_system.domain.trading_rules import SymbolRules
from trading_system.evaluation.runner import run_evaluation
from trading_system.reconciliation.spot import initial_baseline
from trading_system.storage.repository import OrderIdentity, Repository
from trading_system.strategy.m2_verify import SPEC, M2Verify

ROOT = Path(__file__).resolve().parents[2]
D = Decimal
SYM = "BTCUSDT"
ENTRY_AT = datetime(2026, 10, 20, 1, 0, tzinfo=UTC)    # KST 10:00
EXIT_AT = datetime(2026, 10, 20, 4, 0, tzinfo=UTC)     # KST 13:00
PARAMS = {"notional": D("10"), "qty_step": D("0.00001"), "split_time": "02:30"}
EVAL_TIMES = (time(1, 0), time(4, 0))                  # UTC (D-207) — 제안 기본값
LAST = D("60000.00")   # 가상 가격 (테스트넷 실제 시세 아님)
CAPS = load_capabilities(ROOT / "docs" / "capabilities.md")

# 테스트넷 BTCUSDT 거래 규칙 실측값 (BN §12, 2026-10-14). 가격·잔고 아님
BN_RULES = SymbolRules(
    instrument=SYM, tick_size=D("0.01"), min_price=D("0.01"), max_price=D("1000000"),
    lot_min_qty=D("0.00001"), lot_max_qty=D("9000"), lot_step=D("0.00001"),
    market_min_qty=D("0"), market_max_qty=D("133.97193096"), market_step=D("0"),
    min_notional=D("5"), max_notional=D("9000000"), apply_min_to_market=True, apply_max_to_market=False)


def m2_limits_file():  # type: ignore[no-untyped-def]
    """M2 PAPER 한도(A-064, config/m2/limits.paper.toml) — 이 전략이 실제로 받을 계좌 한도."""
    return load_limits_file(ROOT / "config" / "m2" / "limits.paper.toml", market="M2").limits


def params_file() -> dict[str, object]:
    return load_params_file(SPEC, ROOT / "config" / "strategies" / "m2-verify-v1.toml")


def snap(at: datetime, *, ev: str = "ev", next_at: datetime | None = None, **kw: object) -> EvaluationSnapshot:
    base: dict = {"instrument": SYM, "daily_bars": (Bar(date(2026, 10, 19), *(D("59000"),) * 4, D("1")),),
                  "last_price": LAST}
    base.update(kw)
    return EvaluationSnapshot(ev, "M2", ACCOUNT, at, D("10000"), (InstrumentView(**base),), next_evaluation_at=next_at)


MINE = PositionView(D("0.00015"), LAST, 0, True)
MANUAL = PositionView(D("1"), D("0"), 0, False)


def signals(at: datetime, params: dict | None = None, **kw: object) -> list[Signal]:
    return M2Verify().evaluate(snap(at, **kw), params or PARAMS)


# --- 규칙 -----------------------------------------------------------------------------------------

def test_entry_time_flat_enters_with_floored_quantity_and_exit_time_holding_exits() -> None:
    [s] = signals(ENTRY_AT)
    assert (s.kind, s.rule_id, s.order_type, s.quantity, s.price) == \
        (RequestType.ENTRY, "ENTRY-1", OrderType.LIMIT, D("0.00016"), LAST)   # 10 ÷ 60000 = 0.0001666.. → 0.00016
    assert s.valid_until == ENTRY_AT + timedelta(minutes=10)
    [x] = signals(EXIT_AT, position=MINE)
    assert (x.kind, x.rule_id, x.order_type, x.quantity) == (RequestType.EXIT, "EXIT-1", OrderType.MARKET, D("0.00015"))


def test_entry_time_holding_does_nothing_and_exit_time_flat_does_nothing() -> None:
    """청산을 놓친 다음 날: 01:00에는 아무것도 안 함(진입·청산 둘 다), 04:00에 청산 — 순서 뒤집힘 없음 (M1 v2 교훈)."""
    assert signals(ENTRY_AT, position=MINE) == []
    assert signals(EXIT_AT) == []


def test_manual_or_foreign_holding_blocks_both_rules() -> None:
    """테스트넷 지급 BTC(기준선 확정·리셋 확인 → 소유자 manual)는 건드리지 않고, 그 위에 진입하지도 않는다 (INV-16)."""
    assert signals(ENTRY_AT, position=MANUAL) == []
    assert signals(EXIT_AT, position=MANUAL) == []


def test_split_boundary_and_timezone() -> None:
    at_split = datetime(2026, 10, 20, 2, 30, tzinfo=UTC)
    assert [s.kind for s in signals(at_split, position=MINE)] == [RequestType.EXIT]   # split_time 시각 = 청산 판정
    assert [s.kind for s in signals(ENTRY_AT.astimezone(KST))] == [RequestType.ENTRY]  # KST 10:00 = UTC 01:00


@pytest.mark.parametrize("kw", [
    {"data_ok": False}, {"has_pending_order": True}, {"last_price": None},
    {"statuses": frozenset({next(iter(TradingStatus))})}, {"instrument": "ETHUSDT"},
])
def test_no_entry_when_inputs_not_clean(kw: dict) -> None:
    assert signals(ENTRY_AT, **kw) == []


def test_exit_uses_daily_close_when_no_last_price_but_not_on_pending_or_bad_data() -> None:
    [x] = signals(EXIT_AT, position=MINE, last_price=None)
    assert x.price == D("59000")
    assert signals(EXIT_AT, position=MINE, has_pending_order=True) == []
    assert signals(EXIT_AT, position=MINE, data_ok=False) == []
    assert signals(EXIT_AT, position=MINE, last_price=None, daily_bars=()) == []


# --- 수량 (S-1, qty_step 내림, minNotional) -----------------------------------------------------------

@pytest.mark.parametrize("price,qty", [
    ("60000.00", "0.00016"), ("99999.99", "0.0001"), ("100000.00", "0.0001"), ("500000.01", "0.00001"),
    ("1000000.00", "0.00001"), ("1000000.01", None), ("12.34", "0.81037"),
])
def test_quantity_is_notional_over_price_floored_to_step(price: str, qty: str | None) -> None:
    out = signals(ENTRY_AT, last_price=D(price))
    assert [s.quantity for s in out] == ([] if qty is None else [D(qty)])


@pytest.mark.parametrize("price", ["9999.99", "66666.67", "333333.34", "500000.01", "999999.99"])
@pytest.mark.parametrize("notional", ["10", "10.01", "17", "50"])
def test_floored_order_value_stays_above_min_notional(price: str, notional: str) -> None:
    """notional ≥ 2 × minNotional(5)이면 qty > 0일 때 qty × 최근가 > 5 — 내림 손실은 notional의 절반 미만."""
    for s in signals(ENTRY_AT, {**PARAMS, "notional": D(notional)}, last_price=D(price)):
        assert s.quantity % D("0.00001") == 0 and s.quantity * D(price) > D("5")
        assert s.quantity * D(price) <= D(notional)


# --- 배포·규칙 검사 ---------------------------------------------------------------------------------

@pytest.mark.parametrize("split,times,ok", [
    ("02:30", EVAL_TIMES, True),
    ("02:30", (time(1, 0), time(4, 0), time(7, 0)), True),     # 청산 판정 여러 번은 허용
    ("02:30", (time(0, 30), time(1, 0), time(4, 0)), False),   # 진입 판정 2번 → 하루 2회 진입 가능 → 거부
    ("02:30", (time(4, 0),), False),
    ("02:30", (time(1, 0),), False),
    ("04:00", EVAL_TIMES, False),
    ("4:00", (time(1, 0), time(5, 0)), False),
])
def test_validate_deploy(split: str, times: tuple[time, ...], ok: bool) -> None:
    assert (M2Verify.validate_deploy({**PARAMS, "split_time": split}, times) == []) is ok


def test_validate_rules_against_measured_testnet_rules() -> None:
    """DP-54 형식(params, LOT_SIZE stepSize, minNotional). §12 실측값이면 통과, 바뀌면 시작 거부 사유."""
    assert M2Verify.validate_rules(PARAMS, BN_RULES.lot_step, BN_RULES.min_notional) == []
    assert M2Verify.validate_rules(PARAMS, D("0.0001"), D("5"))          # stepSize가 바뀌면 시작 거부 사유
    assert M2Verify.validate_rules(PARAMS, D("0.00001"), D("6"))         # minNotional이 오르면 notional 10 < 12


def test_start_rule_check_hook_with_measured_rules() -> None:
    """시작 호출부(app.wiring.spot_rule_problems, D-209)를 통해: 실측 규칙 → 문제 없음, 규칙 없음·불일치 → 거부 사유."""
    si = [StrategyInput(M2Verify(), params_file(), EVAL_TIMES)]
    pairs = {SYM: ("BTC", "USDT")}
    assert spot_rule_problems(si, {SYM: BN_RULES}, pairs) == []
    assert spot_rule_problems(si, {}, pairs) == ["m2-verify:1 BTCUSDT: 거래 규칙 없음"]
    from dataclasses import replace
    [p] = spot_rule_problems(si, {SYM: replace(BN_RULES, lot_step=D("0.0001"))}, pairs)
    assert "qty_step" in p


# --- 명세·파라미터 파일·등록 ------------------------------------------------------------------------

def test_params_file_valid_and_matches_proposed_defaults() -> None:
    params = params_file()
    assert params == {"notional": D("10"), "qty_step": D("0.00001"), "split_time": "02:30"}
    assert M2Verify.validate_deploy(params, EVAL_TIMES) == []


def test_registration_with_real_capabilities_and_m2_paper0_exception() -> None:
    """실제 capabilities.md(M2 "검증 중"·CAP-INS-05): BACKTEST 등록 통과, PAPER는 승인 A-ID가 있을 때만(D-212 M2 예외).
    명세의 능력 = M2 PAPER-0 예외 허용 범위 안, 전략 L7 60 ≤ 계좌 L7 100."""
    assert set(SPEC.capabilities) <= PAPER0_M2_CAPABILITIES and "CAP-INS-05" in SPEC.capabilities
    assert validate_spec(SPEC, CAPS, m2_limits_file(), Mode.BACKTEST) == []
    refused = validate_spec(SPEC, CAPS, m2_limits_file(), Mode.PAPER)
    assert refused and any("검증 중" in p for p in refused), refused
    assert validate_spec(SPEC, CAPS, m2_limits_file(), Mode.PAPER, "A-999") == []   # A-999 = 시험용 자리 값


def _deploy_root(tmp: Path, mode: Mode, *, qty_step: str = "0.00001", approval: str | None = None) -> Path:
    """M2 체크아웃 흉내 + m2-verify 배포 설정(제안 기본값). 비밀 값 없음."""
    root = make_m2_root(tmp, mode, symbols='quote_asset = "USDT"\nsymbols = ["BTCUSDT"]\n')
    (root / "docs").mkdir()
    shutil.copy(ROOT / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    (root / "config" / "strategies").mkdir()
    src = (ROOT / "config" / "strategies" / "m2-verify-v1.toml").read_text(encoding="utf-8")
    (root / "config" / "strategies" / "m2-verify-v1.toml").write_text(
        src.replace('qty_step = "0.00001"', f'qty_step = "{qty_step}"'), encoding="utf-8")
    app = root / "config" / "m2" / f"app.{mode.value.lower()}.toml"
    app.write_text(app.read_text(encoding="utf-8") + '\n[[strategies]]\nmodule = "trading_system.strategy.m2_verify:'
                   'M2Verify"\nparams = "config/strategies/m2-verify-v1.toml"\neval_times = ["01:00", "04:00"]\n'
                   + (f'verification_approval = "{approval}"\n' if approval else ""), encoding="utf-8")
    return root


def _states(db: Path, mode: Mode) -> list[dict]:
    repo = Repository(db, mode, order_identity=OrderIdentity.SYMBOL)
    try:
        return [dict(r) for r in repo.strategy_states()]
    finally:
        repo.close()


def test_registration_path_strategy_tool_market_m2_paper(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """`scripts/strategy.py register --mode PAPER --market M2` (D-212 (1)): 배포 설정의 verification_approval로 등록 통과,
    M2 DB(SYMBOL 식별·M2 메타)에 시장 M2·DRAFT 행. 승인 A-ID가 없으면 등록 거부(검증 중 능력은 PAPER 불가)."""
    root = _deploy_root(tmp_path, Mode.PAPER, approval="A-999")
    assert strategy_main(["register", "--mode", "PAPER", "--market", "M2", "--root", str(root)]) == 0
    assert "등록 통과: m2-verify:1" in capsys.readouterr().out
    [row] = _states(root / "var" / "paper" / "trading.db", Mode.PAPER)
    assert (row["strategy_id"], row["version"], row["market"], row["state"]) == ("m2-verify", 1, "M2", "DRAFT")

    other = _deploy_root(tmp_path / "noapproval", Mode.PAPER)
    assert strategy_main(["register", "--mode", "PAPER", "--market", "M2", "--root", str(other)]) == 1
    assert "등록 거부: m2-verify:1" in capsys.readouterr().out


def test_registration_path_market_m1_refuses_m2_strategy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--market을 빼면(M1 기본) M2 배포 설정을 읽지 않는다 — M1 설정 파일이 없으니 등록 거부(M2 전략이 M1 DB로 가지 않음)."""
    root = _deploy_root(tmp_path, Mode.PAPER, approval="A-999")
    assert strategy_main(["register", "--mode", "PAPER", "--root", str(root)]) != 0
    assert not (root / "var" / "paper" / "trading.db").exists() or \
        _states(root / "var" / "paper" / "trading.db", Mode.PAPER) == []


def test_run_m2_backtest_starts_with_measured_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                    capsys: pytest.CaptureFixture[str]) -> None:
    """`run --market M2` 시작 순서(D-209): 규칙 적재(실측값) → validate_rules 통과 → DB → 등록(BACKTEST) → K8. 주문 0."""
    root = _deploy_root(tmp_path, Mode.BACKTEST)
    monkeypatch.setattr(app_main, "_m2_exchange_rules", lambda mode, symbols: ({SYM: BN_RULES}, None))
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 0
    out = capsys.readouterr().out
    assert "시작: 시장 M2" in out and "K8" in out
    db = root / "var" / "backtest" / "trading.db"
    [row] = _states(db, Mode.BACKTEST)
    assert (row["strategy_id"], row["market"]) == ("m2-verify", "M2")
    repo = Repository(db, Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        assert repo.select("SELECT COUNT(*) AS n FROM intent")[0]["n"] == 0
        assert "K8" in repo.active_halts("BN-BT-01")   # 사용자 재개 전 — 판정·주문 없음 (D-209)
    finally:
        repo.close()


def test_run_m2_refused_when_params_disagree_with_exchange_rules(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                                 capsys: pytest.CaptureFixture[str]) -> None:
    root = _deploy_root(tmp_path, Mode.BACKTEST, qty_step="0.0001")
    monkeypatch.setattr(app_main, "_m2_exchange_rules", lambda mode, symbols: ({SYM: BN_RULES}, None))
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    out = capsys.readouterr().out
    assert "규칙 불일치" in out and "qty_step" in out and "BTCUSDT" in out
    assert not (root / "var" / "backtest" / "trading.db").exists()


def test_runner_keeps_signals_and_drops_non_step_exit() -> None:
    """판정 실행기: 수량 단위(lot_of = LOT_SIZE stepSize) 배수만 통과. M2 판정 입력(D-207)은 청산 가능 수량을 LOT_SIZE로
    내려 보여 주므로(D-208) 실제 경로에서는 이 버림이 생기지 않는다 — 내리지 않은 값이 오면 버려짐을 고정."""
    lot = {SYM: D("0.00001")}
    ev = evaluation_id_of(SPEC.strategy_id, SPEC.version, "M2", "2026-10-20T01:00Z")
    r = run_evaluation(M2Verify(), snap(ENTRY_AT, ev=ev, next_at=EXIT_AT), PARAMS, lot_of=lot)
    assert [s.kind for s in r.signals] == [RequestType.ENTRY] and r.dropped == []
    raw = PositionView(D("0.00015984"), LAST, 0, True)
    r = run_evaluation(M2Verify(), snap(EXIT_AT, ev=ev, position=raw), PARAMS, lot_of=lot)
    assert r.signals == [] and "수량 단위" in r.dropped[0].reason


# --- 하루 왕복: build_engine(market="M2") + fake 현물 브로커 (실운영 조립과 같은 함수, D-202) ---------------------------

@pytest.fixture
def w(tmp_path: Path) -> Iterator[M2World]:
    world = M2World(tmp_path, sym=SYM, base_asset="BTC", price=str(LAST), symbol_rule=BN_RULES, caps=CAPS,
                    limits=m2_limits_file(), strategies=[StrategyInput(M2Verify(), params_file(), EVAL_TIMES)])
    yield world
    world.close()   # 불변 조건 위반 0 확인


def _qty(w: M2World) -> Decimal:
    return w.repo.position(ACCOUNT, SYM).quantity


def test_engine_day_round_trip_fee_dust_and_next_day_reentry(w: M2World) -> None:
    w.set_baseline()
    w.start_and_resume()                      # UTC 00:30 시작 → K8 → 시작 대조 → 사용자 재개
    w.tick_at(0, 59)
    assert w.sent() == []
    w.tick_at(1, 0)                           # UTC 01:00 진입 판정 (KST 10:00)
    [buy] = w.sent()
    assert (buy.side, buy.order_type, buy.quantity, buy.price) == ("BUY", OrderType.LIMIT, D("0.00016"), D("60300.00"))
    w.tick_at(1, 2)                           # 체결 반영 + 주기 대조
    assert _qty(w) == D("0.00016") - D("0.00000016")                 # 수수료(가상 0.1%)는 기초 자산에서 (D-101)
    assert w.repo.active_ownership(ACCOUNT, SYM).owner == "m2-verify:1"

    w.tick_at(2, 30)
    w.tick_at(3, 59)
    assert len(w.sent()) == 1                 # split_time·사이 시각에는 판정 없음
    w.tick_at(4, 0)                           # UTC 04:00 청산 판정 (KST 13:00)
    sell = w.sent()[-1]
    assert (sell.side, sell.order_type, sell.quantity) == ("SELL", OrderType.MARKET, D("0.00015"))   # D-208 내림
    w.tick_at(4, 2)                           # 다음 대조·dust 점검
    assert _qty(w) == D("0.00000984") and w.repo.active_ownership(ACCOUNT, SYM) is None   # dust → 소유권 해제 (D-102)

    nxt = w.day + timedelta(days=1)
    w.tick_at(0, 30, day=nxt)                 # UTC 일 경계 지남
    w.tick_at(1, 0, day=nxt)                  # dust는 미보유 → 다음 날 재진입
    assert [r.side for r in w.sent()] == ["BUY", "SELL", "BUY"]
    owners = {i["owner"] for i in w.intents()}
    assert owners == {"m2-verify:1"}


def test_engine_ioc_zero_fill_expires_no_exit_no_rebuy_same_day(w: M2World) -> None:
    """PV-M2-11 경로: IOC 체결 0 → EXPIRED, 예약 해제, 04:00 청산 없음, 같은 날 재매수 없음(진입 판정 1회)."""
    w.broker.ioc_liquidity[SYM] = D("0")
    w.set_baseline()
    w.start_and_resume()
    w.tick_at(1, 0)
    w.tick_at(1, 2)
    assert len(w.sent()) == 1 and _qty(w) == 0 and w.repo.active_reservations(ACCOUNT) == []
    w.tick_at(4, 0)
    w.tick_at(23, 0)
    assert len(w.sent()) == 1


def _grant_btc_and_baseline(w: M2World) -> None:
    """테스트넷 지급 BTC 1개(BN §12) + 기준선 확정(D-210 initial-baseline과 같은 공통 함수) → BTCUSDT 수동 보유."""
    w.broker.grant["BTC"] = D("1")
    initial_baseline(w.repo, w.clock, w.broker.query_balances(ACCOUNT), ACCOUNT, universe=w.pairs, quote_asset="USDT",
                     existing_baseline=None, set_baseline=w.engine.account_state.set_baseline,  # type: ignore[attr-defined]
                     operator="tester")
    assert w.repo.active_ownership(ACCOUNT, SYM).owner == "manual"


def test_engine_granted_btc_blocks_strategy_until_operator_exit(w: M2World) -> None:
    """A-106 Q-1 전제: 지급 BTC를 팔지 않으면 전략은 진입하지 않는다(수동 보유 — 신호 없음, 주문 0)."""
    _grant_btc_and_baseline(w)
    w.start_and_resume()
    w.tick_at(1, 0)
    w.tick_at(4, 0)
    assert w.sent() == []


def test_engine_granted_btc_exit_right_after_start_then_strategy_runs(w: M2World) -> None:
    """A-106 Q-1 (가) 절차: 기준선(지급 BTC = 수동 보유) → 시작·K8 재개 → 운영 요청 EXIT BTCUSDT 1(텔레그램 /exit와 같은
    명령) → 시장가 매도 1, 보유 0·소유권 해제 → UTC 01:00 전략 진입. 위험 축소라 L7(100 USDT)은 적용되지 않는다(01 D-019)."""
    _grant_btc_and_baseline(w)
    w.start_and_resume()
    w.command("EXIT", {"account": ACCOUNT, "instrument": SYM, "quantity": "1"}, channel="REMOTE")
    w.tick_at(0, 40)
    [sell] = w.sent()
    assert (sell.side, sell.order_type, sell.quantity) == ("SELL", OrderType.MARKET, D("1"))
    w.tick_at(0, 42)
    assert _qty(w) == 0 and w.repo.active_ownership(ACCOUNT, SYM) is None
    w.tick_at(1, 0)
    assert [(r.side, r.order_type) for r in w.sent()] == [("SELL", OrderType.MARKET), ("BUY", OrderType.LIMIT)]
    assert w.repo.active_ownership(ACCOUNT, SYM).owner == "m2-verify:1"
