"""T-071 기준선 이후 유니버스 추가 도구 `universe-add` (CP-041 D-273·D-280, V-343·V-351 일부).

네트워크 없음: 가짜 현물 브로커(가짜 잔고)·임시 저장소 루트·BACKTEST(fake 연습, 사람이 적는 가상 잔고)만 쓴다.
심볼·계좌 별칭·수량·승인 번호는 모두 가상값(시험 전용)이다. 실제 DB에는 실행하지 않는다(D-272 ⑥은 사용자).
"""

from __future__ import annotations

import sqlite3
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t049_m2_ops import _baseline, _m2_root_with_strategy
from tests.support.m2_engine_world import ACCOUNT, SYM, M2World
from tests.support.m2_fakes import FEE_RATE
from trading_system.app.m2_baseline import m2_baseline_main
from trading_system.broker.fake.broker import FakeBehavior, FakeSpotFees
from trading_system.domain.enums import Mode
from trading_system.domain.market_time import Market
from trading_system.reconciliation.spot import UniverseAddError, add_universe_asset
from trading_system.storage.market_meta import bind_market
from trading_system.storage.repository import OrderIdentity, Repository

D = Decimal
SYM_B = "TSTB-USDT"                       # 기준선 뒤 유니버스에 넣는 가상 심볼 (ETHUSDT 자리)
TWO_PAIRS = {SYM: ("TSTA", "USDT"), SYM_B: ("TSTB", "USDT")}
OWNER = "t-m2fixed:1"                     # 전략 소유 보유 (m2-verify 소유 BTC 자리)
HELD = D("0.0100")
GRANT_B = D("1.5")                        # 테스트넷 지급 흉내 (ETH x 자리)


# --- 엔진 세계 (L2): 기준선 있는 M2 DB + 전략 소유 보유 → 유니버스에 심볼 추가 ---------------------------------------

def _world_with_strategy_holding(tmp: Path) -> M2World:
    """기준선 확정 + 전략(OWNER) 소유 TSTA 보유, 거래소·내부 같음. 시작 대조 MATCH까지 확인한 뒤 엔진 '정지'(DB 닫음)."""
    w = M2World(tmp, strategies=[])
    w.set_baseline()
    w.broker.grant["TSTA"] = HELD
    with w.repo.tx():
        w.repo.set_position(ACCOUNT, SYM, HELD, D("10000"))
        w.repo.insert_ownership(ACCOUNT, SYM, OWNER, None, w.clock.now())
        own = w.repo.active_ownership(ACCOUNT, SYM)
        w.repo.set_ownership_state(own.ownership_id, "CONFIRMED", None)
    w.engine.start()
    assert _last_recon(w) == "MATCH"
    return w


def _add_symbol_to_universe(w: M2World) -> None:
    """D-272 ④ 흉내: 엔진 정지 중 설정 유니버스에 심볼 추가 + 거래소 지급 잔고. 다음 build가 두 심볼로 조립된다."""
    w.repo.close()
    w.pairs = dict(TWO_PAIRS)
    w.broker.spot_fees = FakeSpotFees(rate=FEE_RATE, pairs=w.pairs)
    w.broker.grant["TSTB"] = GRANT_B
    w.engine = w.build()


def _last_recon(w: M2World) -> str:
    return w.repo.select("SELECT result FROM reconciliation ORDER BY created_seq DESC LIMIT 1")[0]["result"]


def _snapshot(repo: Repository) -> tuple:
    """쓰기 여부 확인용 표 사본 (감사·포지션·소유권·대조 항목)."""
    return tuple(repo.select(f"SELECT * FROM {t} ORDER BY rowid") for t in
                 ("audit_event", "position", "ownership", "recon_item"))


def _add(w: M2World, symbol: str = SYM_B):  # type: ignore[no-untyped-def]
    return add_universe_asset(w.repo, w.clock, w.broker.query_balances(ACCOUNT), ACCOUNT, instrument=symbol,
                              universe=w.pairs, existing_baseline=w.engine.account_state.baseline(ACCOUNT),  # type: ignore[attr-defined]
                              operator="tester", approval="A-901")


def test_v343_universe_add_records_manual_holding_keeps_baseline_and_ownership(tmp_path: Path) -> None:
    w = _world_with_strategy_holding(tmp_path)
    try:
        _add_symbol_to_universe(w)
        base_before = w.engine.account_state.baseline(ACCOUNT)  # type: ignore[attr-defined]
        own_a_before = w.repo.active_ownership(ACCOUNT, SYM)
        n_period = len(w.repo.select("SELECT 1 FROM audit_event WHERE kind LIKE 'PAPER_PERIOD_%'"))
        n_base_ev = len(w.audits("SPOT_QUOTE_BASELINE"))

        rep = _add(w)

        assert (rep.instrument, rep.base_asset, rep.quantity, rep.manual) == (SYM_B, "TSTB", GRANT_B, True)
        assert w.repo.position(ACCOUNT, SYM_B).quantity == GRANT_B                      # 수동 보유 x
        own_b = w.repo.active_ownership(ACCOUNT, SYM_B)
        assert own_b is not None and (own_b.owner, own_b.state) == ("manual", "CONFIRMED")
        [ev] = w.audits("UNIVERSE_ASSET_ADDED")                                         # 감사 1건
        assert '"instrument": "TSTB-USDT"' in ev["summary"] and '"quantity": "1.5"' in ev["summary"]
        assert '"approval": "A-901"' in ev["summary"] and ev["actor"] == "tester"
        # USDT 기준선·다른 심볼 포지션·소유권 불변, PAPER 기간 재시작 기록 0
        assert w.engine.account_state.baseline(ACCOUNT) == base_before  # type: ignore[attr-defined]
        assert len(w.audits("SPOT_QUOTE_BASELINE")) == n_base_ev
        assert w.repo.position(ACCOUNT, SYM).quantity == HELD
        assert w.repo.active_ownership(ACCOUNT, SYM) == own_a_before and own_a_before.owner == OWNER
        assert len(w.repo.select("SELECT 1 FROM audit_event WHERE kind LIKE 'PAPER_PERIOD_%'")) == n_period
        assert not w.audits("TESTNET_RESET_CONFIRMED") and not w.audits("INITIAL_BASELINE_CONFIRMED")

        w.engine.start()                                                                 # 다음 대조 MATCH
        assert _last_recon(w) == "MATCH"
        assert "K4" not in w.repo.active_halts(ACCOUNT)
    finally:
        w.repo.close()


def test_v343_zero_balance_records_zero_position_without_ownership(tmp_path: Path) -> None:
    w = _world_with_strategy_holding(tmp_path)
    try:
        _add_symbol_to_universe(w)
        w.broker.grant.pop("TSTB")
        rep = _add(w)
        assert rep.quantity == 0 and rep.manual is False
        assert w.repo.select("SELECT quantity FROM position WHERE instrument=?", (SYM_B,)) == [{"quantity": "0"}]
        assert w.repo.active_ownership(ACCOUNT, SYM_B) is None
        assert len(w.audits("UNIVERSE_ASSET_ADDED")) == 1
        w.engine.start()
        assert _last_recon(w) == "MATCH"
    finally:
        w.repo.close()


def test_v343_without_tool_start_gives_qty_mismatch_and_k4(tmp_path: Path) -> None:
    """도구 없이 시작 = 현행 동작 기록: 새 심볼 지급분이 내부 0과 달라 QTY_MISMATCH → K4 (D-272 P-3)."""
    w = _world_with_strategy_holding(tmp_path)
    try:
        _add_symbol_to_universe(w)
        w.engine.start()
        assert _last_recon(w) == "MISMATCH"
        items = w.repo.open_recon_items(ACCOUNT)
        assert [(i["kind"], i["instrument"], i["internal_value"], i["broker_value"]) for i in items] == [
            ("QTY_MISMATCH", SYM_B, "0", "1.5")]
        assert "K4" in w.repo.active_halts(ACCOUNT)
    finally:
        w.repo.close()


@pytest.mark.parametrize(("symbol", "needle"), [(SYM, "이미 있는 심볼"), ("TSTC-USDT", "유니버스 밖")])
def test_v343_existing_or_outside_symbol_refused_nothing_written(tmp_path: Path, symbol: str, needle: str) -> None:
    w = _world_with_strategy_holding(tmp_path)
    try:
        _add_symbol_to_universe(w)
        before = _snapshot(w.repo)
        with pytest.raises(UniverseAddError, match=needle):
            _add(w, symbol)
        assert _snapshot(w.repo) == before
    finally:
        w.repo.close()


def test_v343_symbol_with_ownership_history_only_refused(tmp_path: Path) -> None:
    """포지션 행이 없어도 소유권 기록(해제된 것 포함)이 있으면 '이미 있는 심볼'."""
    w = _world_with_strategy_holding(tmp_path)
    try:
        _add_symbol_to_universe(w)
        with w.repo.tx():
            w.repo.insert_ownership(ACCOUNT, SYM_B, "manual", None, w.clock.now())
            own = w.repo.active_ownership(ACCOUNT, SYM_B)
            w.repo.set_ownership_state(own.ownership_id, "RELEASED", "시험", w.clock.now())
        before = _snapshot(w.repo)
        with pytest.raises(UniverseAddError, match="이미 있는 심볼"):
            _add(w)
        assert _snapshot(w.repo) == before
    finally:
        w.repo.close()


def test_v343_without_baseline_refused(tmp_path: Path) -> None:
    w = M2World(tmp_path, strategies=[])
    try:
        w.repo.close()
        w.pairs = dict(TWO_PAIRS)
        w.engine = w.build()
        before = _snapshot(w.repo)
        with pytest.raises(UniverseAddError, match="initial-baseline"):
            _add(w)
        assert _snapshot(w.repo) == before
    finally:
        w.repo.close()


def test_v343_active_intent_refused(tmp_path: Path) -> None:
    w = M2World(tmp_path)                                      # 기본 고정 전략 (01:00 매수)
    try:
        w.set_baseline()
        w.start_and_resume()
        w.broker.behavior = FakeBehavior.UNKNOWN
        w.tick_at(1, 0)                                         # 결과 불명 의도
        w.pairs = dict(TWO_PAIRS)
        before = _snapshot(w.repo)
        with pytest.raises(UniverseAddError, match="활성 의도"):
            _add(w)
        assert _snapshot(w.repo) == before
    finally:
        w.repo.close()


def test_v343_open_order_refused(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    w = _world_with_strategy_holding(tmp_path)
    try:
        _add_symbol_to_universe(w)
        monkeypatch.setattr(w.repo, "open_orders", lambda account: [object()])   # 열린 내부 주문 (합성)
        before = _snapshot(w.repo)
        with pytest.raises(UniverseAddError, match="열린 내부 주문"):
            _add(w)
        assert _snapshot(w.repo) == before
    finally:
        w.repo.close()


def test_v343_no_remote_universe_add_command(tmp_path: Path) -> None:
    """원격(텔레그램) 경로 없음: 문구에도 없고, 운영 명령 표로 와도 처리기가 모름 → 거부, 아무것도 쓰지 않음."""
    from trading_system.ops import telegram
    text = Path(telegram.__file__).read_text(encoding="utf-8")
    assert "universe-add" not in text and "UNIVERSE_ADD" not in text and "universe_add" not in text
    w = M2World(tmp_path, strategies=[])
    try:
        w.set_baseline()
        w.start_and_resume()
        w.command("UNIVERSE_ADD", {"account": ACCOUNT, "symbol": SYM_B}, channel="REMOTE")
        w.tick_at(1, 0)
        [cmd] = w.repo.select("SELECT status FROM ops_command WHERE command='UNIVERSE_ADD'")
        assert cmd["status"] == "REJECTED"
        assert not w.audits("UNIVERSE_ASSET_ADDED")
    finally:
        w.repo.close()


# --- 도구 (scripts/reconcile-resolve.py universe-add → app.m2_baseline) ------------------------------------------------

TWO_UNIVERSE = 'quote_asset = "USDT"\nsymbols = ["TSTAUSDT", "TSTBUSDT"]\n'


def _root_after_baseline(tmp: Path) -> Path:
    """BACKTEST(fake 연습) 루트: TSTAUSDT만으로 기준선 확정 → 유니버스에 TSTBUSDT 추가(D-272 ④) + 승인 기록 표."""
    root = _m2_root_with_strategy(tmp)
    assert _baseline(root, "initial-baseline", "--quote-amount", "10000") == 0
    (root / "config" / "m2" / "universe.backtest.toml").write_text(TWO_UNIVERSE, encoding="utf-8")
    (root / "docs" / "approvals.md").write_text("| A-ID | 날짜 |\n|---|---|\n| A-901 | 2026-10-19 |\n", encoding="utf-8")
    return root


def _ua(root: Path, *args: str) -> int:
    return m2_baseline_main(["universe-add", "--mode", "BACKTEST", "--market", "M2", "--operator", "tester", *args], root)


def _db(root: Path) -> Path:
    return root / "var" / "backtest" / "trading.db"


def _tables(db: Path) -> tuple:
    conn = sqlite3.connect(db)
    try:
        return tuple(conn.execute(f"SELECT * FROM {t} ORDER BY rowid").fetchall() for t in
                     ("audit_event", "position", "ownership", "recon_item", "meta"))
    finally:
        conn.close()


OK_ARGS = ("--symbol", "TSTBUSDT", "--approval", "A-901", "--base-amount", "1.5")


def test_v343_tool_backtest_adds_symbol(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _root_after_baseline(tmp_path)
    capsys.readouterr()
    assert _ua(root, *OK_ARGS) == 0
    out = capsys.readouterr().out
    assert "universe-add 완료: TSTBUSDT" in out and "K4는 풀지 않았다" in out
    repo = Repository(_db(root), Mode.BACKTEST, order_identity=OrderIdentity.SYMBOL)
    try:
        kinds = [e["kind"] for e in repo.audit_events()]
        assert kinds.count("UNIVERSE_ASSET_ADDED") == 1
        assert kinds.count("SPOT_QUOTE_BASELINE") == 1 and "PAPER_PERIOD_RESTART" not in kinds
        assert kinds.count("PAPER_PERIOD_START") == 1
        assert repo.position("BN-BT-01", "TSTBUSDT").quantity == D("1.5")
        assert repo.active_ownership("BN-BT-01", "TSTBUSDT").owner == "manual"
    finally:
        repo.close()
    assert _ua(root, *OK_ARGS) == 2                                         # 두 번째 = 이미 있는 심볼
    assert "이미 있는 심볼" in capsys.readouterr().out


@pytest.mark.parametrize(("args", "needle"), [
    (("--symbol", "TSTCUSDT", "--approval", "A-901", "--base-amount", "1"), "유니버스 밖"),
    (("--symbol", "TSTAUSDT", "--approval", "A-901", "--base-amount", "1"), "이미 있는 심볼"),
    (("--symbol", "TSTBUSDT", "--base-amount", "1"), "승인"),
    (("--symbol", "TSTBUSDT", "--approval", "A-999", "--base-amount", "1"), "승인"),
    (("--symbol", "TSTBUSDT", "--approval", "A-901"), "--base-amount"),
    (("--symbol", "TSTBUSDT", "--approval", "A-901", "--base-amount", "-1"), "--base-amount"),
    (("--symbol", "TSTBUSDT", "--approval", "A-901", "--base-amount", "1", "--quote-amount", "1"), "--quote-amount"),
])
def test_v343_tool_refusals_write_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                          args: tuple[str, ...], needle: str) -> None:
    root = _root_after_baseline(tmp_path)
    before = _tables(_db(root))
    capsys.readouterr()
    assert _ua(root, *args) == 2
    out = capsys.readouterr().out
    assert "거부(아무것도 쓰지 않음)" in out and needle in out
    assert _tables(_db(root)) == before


def test_v343_tool_refused_while_engine_running(tmp_path: Path) -> None:
    from trading_system.app.instance_lock import acquire_instance_lock
    root = _root_after_baseline(tmp_path)
    before = _db(root).read_bytes()
    lock = acquire_instance_lock(_db(root).parent)
    try:
        assert _ua(root, *OK_ARGS) == 2
    finally:
        lock.release()
    assert _db(root).read_bytes() == before


def test_v343_tool_without_db_refused_and_creates_nothing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _m2_root_with_strategy(tmp_path)
    (root / "config" / "m2" / "universe.backtest.toml").write_text(TWO_UNIVERSE, encoding="utf-8")
    (root / "docs" / "approvals.md").write_text("| A-901 | 2026-10-19 |\n", encoding="utf-8")
    assert _ua(root, *OK_ARGS) == 2
    assert "initial-baseline" in capsys.readouterr().out
    assert not _db(root).exists()


def test_v343_v351_tool_refuses_m1_db_and_m1_market(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = _m2_root_with_strategy(tmp_path)
    (root / "config" / "m2" / "universe.backtest.toml").write_text(TWO_UNIVERSE, encoding="utf-8")
    (root / "docs" / "approvals.md").write_text("| A-901 | 2026-10-19 |\n", encoding="utf-8")
    db = _db(root)
    db.parent.mkdir(parents=True)
    repo = Repository(db, Mode.BACKTEST)
    bind_market(repo.connection, Market.M1, "fake", created=True)
    repo.close()
    before = _tables(db)
    assert _ua(root, *OK_ARGS) == 2
    assert "거부(아무것도 쓰지 않음)" in capsys.readouterr().out
    assert _tables(db) == before
    assert m2_baseline_main(["universe-add", "--mode", "BACKTEST", "--market", "M1", *OK_ARGS], root) == 2
    assert "M2 전용" in capsys.readouterr().out
    assert _tables(db) == before


def test_v343_tool_paper_refuses_base_amount(tmp_path: Path) -> None:
    from tests.scenario.test_tm2_08_market_split import make_m2_root
    root = make_m2_root(tmp_path, Mode.PAPER)
    assert m2_baseline_main(["universe-add", "--mode", "PAPER", "--market", "M2", "--symbol", "TSTBUSDT",
                             "--approval", "A-901", "--base-amount", "1"], root) == 2
    assert not (root / "var").exists()


def test_universe_add_options_refused_on_other_subcommands(tmp_path: Path) -> None:
    root = _m2_root_with_strategy(tmp_path)
    assert _baseline(root, "initial-baseline", "--quote-amount", "10000", "--symbol", "TSTAUSDT") == 2
    assert not _db(root).exists()
