"""CP-036 T-049 — M2 운영 도구·등록·운영 요청·텔레그램 (D-210·D-211·D-212·D-213). V-288, V-289, V-290.

네트워크 없음. 잔고는 fake 현물 브로커·합성 거래소, 텔레그램은 연결하지 않는다(설정 검사만). 계좌·키·토큰은 가상 값.
"""

from __future__ import annotations

import shutil
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.m2_engine_world import ACCOUNT, SYM, M2World, fixed_spec
from tests.support.m2_fakes import m2_limits
from trading_system.app.m2_baseline import m2_baseline_main
from trading_system.app.strategy_tool import strategy_main
from trading_system.app.wiring import load_strategy
from trading_system.config.app_file import load_app_settings
from trading_system.config.capabilities import load_capabilities
from trading_system.config.limits_file import load_limits_file
from trading_system.config.strategy_registry import PAPER0_M2_CAPABILITIES, validate_spec
from trading_system.domain.enums import Mode, OrderType
from trading_system.domain.market_time import Market
from trading_system.reconciliation.spot import ResetError, initial_baseline
from trading_system.storage.market_meta import bind_market
from trading_system.storage.repository import OrderIdentity, Repository

D = Decimal
REPO = Path(__file__).resolve().parents[2]
CAPS = load_capabilities(REPO / "docs" / "capabilities.md")

# capabilities.md M1 열 — T-049 변경 전(main 0dc905a) 값. 이 작업은 M1 상태를 하나도 바꾸지 않는다 (V-152)
M1_BEFORE = {
    "CAP-TRG-01": "검증 중", "CAP-TRG-02": "미지원", "CAP-TRG-03": "미지원", "CAP-TRG-04": "미지원",
    "CAP-DAT-01": "검증 중", "CAP-DAT-02": "미지원", "CAP-DAT-03": "미지원", "CAP-DAT-04": "미지원",
    "CAP-DAT-05": "미지원", "CAP-DAT-05a": "검증 중", "CAP-DAT-05b": "해당 없음", "CAP-DAT-05c": "미지원",
    "CAP-DAT-05d": "검증 중", "CAP-ORD-01": "검증 중", "CAP-ORD-02": "검증 중", "CAP-ORD-03": "미지원",
    "CAP-ORD-03a": "미지원", "CAP-ORD-03b": "검증 중", "CAP-ORD-04": "미지원", "CAP-HLD-01": "미지원",
    "CAP-INS-01": "검증 중", "CAP-INS-02": "미지원", "CAP-INS-03": "미지원", "CAP-INS-04": "미지원",
}


# --- capabilities.md M2 열·CAP-INS-05 (D-212 (3)) ---------------------------------------------------------------

def test_capabilities_m1_column_unchanged_and_m2_column_added() -> None:
    m1 = {cap: row["M1"] for cap, row in CAPS.items() if cap != "CAP-INS-05"}
    assert m1 == M1_BEFORE
    assert CAPS["CAP-INS-05"] == {"M1": "해당 없음", "M2": "검증 중", "M3": "해당 없음"}
    for cap in ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02"):
        assert CAPS[cap]["M2"] == "검증 중"
    assert CAPS["CAP-ORD-03a"]["M2"] == CAPS["CAP-ORD-03b"]["M2"] == "미지원"


def test_deployed_m1_paper_config_still_validates() -> None:
    app = load_app_settings(REPO / "config" / "app.paper.toml", Mode.PAPER, REPO)
    limits = load_limits_file(REPO / "config" / "limits.paper.toml").limits
    assert app.strategies
    for dep in app.strategies:
        strat, params = load_strategy(dep)
        assert validate_spec(strat.spec, CAPS, limits, Mode.PAPER, dep.verification_approval) == []


# --- V-289 M2 PAPER-0 검증 예외 (D-212 (2)) ---------------------------------------------------------------------

def _m2_spec(**over: object):  # type: ignore[no-untyped-def]
    from dataclasses import replace
    from trading_system.domain.strategy import StrategyLimits
    base = fixed_spec(strategy_id="t-m2paper0", universe=("BTCUSDT",),
                      capabilities=("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05"),
                      limits=StrategyLimits(l7_max_order_amount=D("100")))
    return replace(base, **over)


def test_v289c_m2_paper0_exception_in_scope_passes() -> None:
    for sizing in ("S-1", "S-4"):
        assert validate_spec(_m2_spec(sizing=sizing), CAPS, m2_limits(), Mode.PAPER, "A-200") == []
    assert PAPER0_M2_CAPABILITIES == frozenset({"CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05"})


@pytest.mark.parametrize("over,approval,needle", [
    ({"capabilities": ("CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-03b", "CAP-INS-05")}, "A-200", "CAP-ORD-03b"),
    ({"markets": ("M1", "M2")}, "A-200", "markets"),
    ({}, None, "검증 중"),
    ({"sizing": "S-2"}, "A-200", "sizing"),
])
def test_v289d_m2_paper0_out_of_scope_refused(over: dict, approval: str | None, needle: str) -> None:
    problems = validate_spec(_m2_spec(**over), CAPS, m2_limits(), Mode.PAPER, approval)
    assert problems and any(needle in p for p in problems), problems


def test_v289d_m2_paper0_requires_l7_and_approval_format() -> None:
    from trading_system.domain.strategy import StrategyLimits
    assert any("l7" in p for p in validate_spec(_m2_spec(limits=StrategyLimits()), CAPS, m2_limits(), Mode.PAPER,
                                                "A-200"))
    assert any("A-ID" in p for p in validate_spec(_m2_spec(), CAPS, m2_limits(), Mode.PAPER, "approved"))


# --- V-289 strategy.py --market (D-212 (1)) ----------------------------------------------------------------------

def _m2_root_with_strategy(tmp: Path, mode: Mode = Mode.BACKTEST) -> Path:
    root = make_m2_root(tmp, mode, symbols='quote_asset = "USDT"\nsymbols = ["TSTAUSDT"]\n')
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    params = root / "config" / "strategies" / "t.toml"
    params.parent.mkdir(parents=True)
    params.write_text('strategy = "t-m2fixed"\nversion = 1\n[params]\nqty = "0.0010"\n', encoding="utf-8")
    app = root / "config" / "m2" / f"app.{mode.value.lower()}.toml"
    app.write_text(app.read_text(encoding="utf-8") + '\n[[strategies]]\nmodule = "tests.scenario.test_t049_m2_ops:'
                   'TstaStrategy"\nparams = "config/strategies/t.toml"\neval_times = ["01:00"]\n', encoding="utf-8")
    return root


class TstaStrategy:
    def __init__(self) -> None:
        from tests.support.m2_engine_world import FixedM2Strategy
        self._s = FixedM2Strategy(fixed_spec(universe=("TSTAUSDT",), capabilities=(
            "CAP-TRG-01", "CAP-DAT-01", "CAP-ORD-01", "CAP-ORD-02", "CAP-INS-05")))
        self.spec = self._s.spec

    def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
        return self._s.evaluate(snapshot, params)


def test_v289a_strategy_tool_market_m2_registers_on_m2_db(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _m2_root_with_strategy(tmp_path)
    assert strategy_main(["register", "--mode", "BACKTEST", "--market", "M2", "--root", str(root)]) == 0
    db = root / "var" / "backtest" / "trading.db"
    repo = Repository(db, Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        [row] = repo.strategy_states()
        assert row["market"] == "M2" and row["state"] == "DRAFT"
    finally:
        repo.close()
    assert strategy_main(["show", "--mode", "BACKTEST", "--market", "M2", "--root", str(root)]) == 0
    assert "M2" in capsys.readouterr().out


def test_v289b_strategy_tool_market_m2_on_m1_db_refused_without_writing(tmp_path: Path,
                                                                         capsys: pytest.CaptureFixture[str]) -> None:
    root = _m2_root_with_strategy(tmp_path)
    db = root / "var" / "backtest" / "trading.db"
    db.parent.mkdir(parents=True)
    repo = Repository(db, Mode.BACKTEST)
    bind_market(repo.connection, Market.M1, "fake", created=True)
    repo.close()
    before = db.read_bytes()
    assert strategy_main(["register", "--mode", "BACKTEST", "--market", "M2", "--root", str(root)]) == 2
    assert "시장" in capsys.readouterr().out and db.read_bytes() == before


def test_v289b_strategy_tool_market_m2_on_db_without_meta_refused(tmp_path: Path) -> None:
    root = _m2_root_with_strategy(tmp_path)
    db = root / "var" / "backtest" / "trading.db"
    db.parent.mkdir(parents=True)
    Repository(db, Mode.BACKTEST).close()                                  # 메타 없는 DB
    assert strategy_main(["register", "--mode", "BACKTEST", "--market", "M2", "--root", str(root)]) == 2


# --- V-288 기준선 확정·리셋 확인 (D-210) ---------------------------------------------------------------------------

def _baseline(root: Path, *args: str) -> int:
    return m2_baseline_main([*args, "--mode", "BACKTEST", "--market", "M2", "--operator", "tester"], root)


def test_v288a_initial_baseline_backtest_virtual_amount(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _m2_root_with_strategy(tmp_path)
    assert _baseline(root, "initial-baseline", "--quote-amount", "10000") == 0
    repo = Repository(root / "var" / "backtest" / "trading.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        kinds = [e["kind"] for e in repo.audit_events()]
        assert "SPOT_QUOTE_BASELINE" in kinds and "INITIAL_BASELINE_CONFIRMED" in kinds and "PAPER_PERIOD_START" in kinds
    finally:
        repo.close()
    assert _baseline(root, "initial-baseline", "--quote-amount", "10000") == 2   # 이미 있음 → reset-confirm
    assert "reset-confirm" in capsys.readouterr().out


def test_v288a_initial_baseline_records_granted_base_as_manual_and_out_of_scope(tmp_path: Path) -> None:
    w = M2World(tmp_path)
    try:
        bal = {"USDT": (D("10000"), D("0")), "TSTA": (D("1"), D("0")), "ETH": (D("1"), D("0"))}
        w.broker.grant.update({"TSTA": D("1"), "ETH": D("1")})                # 테스트넷 지급 흉내 (B-1)
        rep = initial_baseline(w.repo, w.clock, bal, ACCOUNT, universe={SYM: ("TSTA", "USDT")}, quote_asset="USDT",
                               existing_baseline=None, set_baseline=w.engine.account_state.set_baseline,  # type: ignore[attr-defined]
                               operator="tester")
        assert rep.quote_baseline == D("10000") and rep.positions == {SYM: "1"}
        assert w.repo.active_ownership(ACCOUNT, SYM).owner == "manual"
        [ev] = [e for e in w.repo.audit_events() if e["kind"] == "INITIAL_BASELINE_CONFIRMED"]
        assert '"ETH": "1"' in ev["summary"]
        w.engine.start()                                                       # 다음 대조 MATCH
        last = w.repo.select("SELECT result FROM reconciliation ORDER BY created_seq DESC LIMIT 1")[0]["result"]
        assert last == "MATCH"
    finally:
        w.repo.close()


def test_v288b_initial_baseline_refused_with_active_intent(tmp_path: Path) -> None:
    w = M2World(tmp_path)
    try:
        w.set_baseline()
        w.start_and_resume()
        w.broker.behavior = __import__("trading_system.broker.fake.broker", fromlist=["FakeBehavior"]).FakeBehavior.UNKNOWN
        w.tick_at(1, 0)
        with pytest.raises(ResetError, match="활성 의도"):
            initial_baseline(w.repo, w.clock, {"USDT": (D("1"), D("0"))}, ACCOUNT, universe={SYM: ("TSTA", "USDT")},
                             quote_asset="USDT", existing_baseline=None, set_baseline=lambda *a: None,
                             operator="tester")
    finally:
        w.repo.close()


def test_v288d_refused_while_engine_running(tmp_path: Path) -> None:
    from trading_system.app.instance_lock import acquire_instance_lock
    root = _m2_root_with_strategy(tmp_path)
    base = root / "var" / "backtest"
    base.mkdir(parents=True)
    lock = acquire_instance_lock(base)
    try:
        assert _baseline(root, "initial-baseline", "--quote-amount", "10000") == 2
    finally:
        lock.release()
    assert not (base / "trading.db").exists()


def test_v288c_no_remote_baseline_command(tmp_path: Path) -> None:
    from trading_system.ops import telegram
    text = Path(telegram.__file__).read_text(encoding="utf-8")
    assert "baseline" not in text.lower() and "reset-confirm" not in text and "INITIAL_BASELINE" not in text
    w = M2World(tmp_path, strategies=[])                                   # 원격 명령 표로 와도 처리기가 모름 → 거부
    try:
        w.set_baseline()
        w.start_and_resume()
        n_base = sum(e["kind"] == "SPOT_QUOTE_BASELINE" for e in w.repo.audit_events())
        w.command("INITIAL_BASELINE", {"account": ACCOUNT}, channel="REMOTE")
        w.tick_at(1, 0)
        [cmd] = w.repo.select("SELECT status FROM ops_command WHERE command='INITIAL_BASELINE'")
        assert cmd["status"] == "REJECTED"
        assert sum(e["kind"] == "SPOT_QUOTE_BASELINE" for e in w.repo.audit_events()) == n_base
    finally:
        w.repo.close()


def test_v288_paper_requires_injected_balances_and_refuses_amount(tmp_path: Path) -> None:
    root = make_m2_root(tmp_path, Mode.PAPER)
    assert m2_baseline_main(["initial-baseline", "--mode", "PAPER", "--market", "M2", "--quote-amount", "1"], root) == 2
    assert not (root / "var").exists()


def test_v288e_reset_confirm_backtest(tmp_path: Path) -> None:
    root = _m2_root_with_strategy(tmp_path)
    assert _baseline(root, "initial-baseline", "--quote-amount", "10000") == 0
    assert _baseline(root, "reset-confirm", "--quote-amount", "9000") == 0
    repo = Repository(root / "var" / "backtest" / "trading.db", Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        kinds = [e["kind"] for e in repo.audit_events()]
        assert "TESTNET_RESET_CONFIRMED" in kinds and "PAPER_PERIOD_RESTART" in kinds
    finally:
        repo.close()


# --- V-290 운영 요청 /exit·/reduce M2 주문 조건 (D-211) ---------------------------------------------------------------

def _hold(w: M2World) -> None:
    """수동 보유 0.0100 TSTA (외부 지급 흉내 — 거래소·내부 같게)."""
    w.broker.grant["TSTA"] = D("0.0100")
    with w.repo.tx():
        w.repo.set_position(ACCOUNT, SYM, D("0.0100"), D("0"))
        w.repo.insert_ownership(ACCOUNT, SYM, "manual", None, w.clock.now())
        own = w.repo.active_ownership(ACCOUNT, SYM)
        w.repo.set_ownership_state(own.ownership_id, "CONFIRMED", None)


@pytest.fixture
def w(tmp_path: Path):  # type: ignore[no-untyped-def]
    world = M2World(tmp_path, strategies=[])
    world.set_baseline()
    _hold(world)
    world.start_and_resume()
    yield world
    world.repo.close()


def test_v290_exit_manual_holding_market_sell_when_quote_fresh(w: M2World) -> None:
    w.command("EXIT", {"account": ACCOUNT, "instrument": SYM, "quantity": "0.0100"})
    w.tick_at(1, 0)
    [sell] = w.sent()
    assert (sell.side, sell.order_type, sell.quantity) == ("SELL", OrderType.MARKET, D("0.0100"))


def test_v290_exit_stale_quote_ioc_limit(w: M2World) -> None:
    w.command("EXIT", {"account": ACCOUNT, "instrument": SYM, "quantity": "0.0100"})
    w.at(1, 0, quote=False)
    w.clock._now = w.clock.now() + timedelta(seconds=90)  # noqa: SLF001 — 시세 90초 지연
    w.engine.tick()
    [sell] = w.sent()
    assert sell.order_type is OrderType.LIMIT and sell.price < D("10000")


def test_v290_exit_allowed_during_k1(w: M2World) -> None:
    w.command("KILL", {"account": None, "reason": "test"})
    w.tick_at(1, 0)
    w.command("EXIT", {"account": ACCOUNT, "instrument": SYM, "quantity": "0.0100"})
    w.tick_at(1, 1)
    assert [r.side for r in w.sent()] == ["SELL"]


def test_v290_exit_quantity_not_lot_multiple_rejected_no_rounding(w: M2World) -> None:
    w.command("EXIT", {"account": ACCOUNT, "instrument": SYM, "quantity": "0.00995"})
    w.tick_at(1, 0)
    assert w.sent() == []
    [cmd] = w.repo.select("SELECT status, result FROM ops_command WHERE command='EXIT'")
    assert cmd["status"] == "REJECTED" and "LOT" in cmd["result"]


# --- D-213 M2 텔레그램 (기본 꺼짐, M2 전용 봇 키) ---------------------------------------------------------------------

def test_m2_telegram_enabled_requires_m2_bot_keys(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    from trading_system.app.main import run_main
    root = make_m2_root(tmp_path, Mode.PAPER)
    app = root / "config" / "m2" / "app.paper.toml"
    app.write_text(app.read_text(encoding="utf-8") + "\n[paper]\nconnection_approved = true\n[telegram]\nenabled = true\n",
                   encoding="utf-8")
    (root / ".env.paper").write_text("TRADE_MODE=PAPER\nBINANCE_TESTNET_API_KEY=k\nBINANCE_TESTNET_SECRET_KEY=s\n"
                                     "TELEGRAM_BOT_TOKEN=m1-bot\nTELEGRAM_CHAT_ID=1\n", encoding="utf-8")
    assert run_main(root, {"TRADE_MODE": "PAPER"}, market="M2", m2_connector=object()) == 2
    out = capsys.readouterr().out
    assert "M2_TELEGRAM_BOT_TOKEN" in out and "m1-bot" not in out


def test_m2_app_paper_config_in_repo_has_telegram_disabled() -> None:
    import tomllib
    raw = tomllib.loads((REPO / "config" / "m2" / "app.paper.toml").read_text(encoding="utf-8"))
    assert raw["telegram"]["enabled"] is False and raw["paper"]["connection_approved"] is False
    assert "orders_approved" not in raw["paper"]                           # 사용자가 승인 때 직접 추가 (D-216)
