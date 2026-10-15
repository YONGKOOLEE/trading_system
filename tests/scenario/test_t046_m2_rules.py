"""CP-036 T-046 — 시장가 매도 수량 단위(D-208, A-106 Q-4 (가))·M2 판정 입력(D-207)·시작 규칙 검사(D-209, B-8).

검증 V-282(§12 실측 규칙값: LOT_SIZE step 0.00001, MARKET_LOT_SIZE step 0, minNotional 5 — 공개 규칙값),
V-283 (a)(c)(d). 네트워크 없음. 순 수령 0.00015984 → 판정 입력 보유 0.00015 → 시장가 매도 0.00015 → 잔량 0.00000984 = dust.
"""

from __future__ import annotations

import shutil
from dataclasses import replace
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.contract.test_binance_rules import _symbol
from tests.scenario.test_tm2_08_market_split import make_m2_root
from tests.support.m2_engine_world import ACCOUNT, DAY, SYM, M2World
from trading_system.app import main as app_main
from trading_system.app.backtest_m2 import sellable as backtest_sellable
from trading_system.app.main import run_main
from trading_system.broker.binance.facts import to_domain_rules
from trading_system.broker.binance.orders import NewOrder, OrderKind, OrderSide, client_order_id, new_order_params
from trading_system.broker.binance.rules import parse_exchange_info
from trading_system.domain.enums import Mode, OrderType, Side
from trading_system.domain.trading_rules import (
    DustVerdict, dust_verdict, filter_violation, market_qty_step, sellable_quantity,
)
from trading_system.storage.repository import Repository

D = Decimal
REPO = Path(__file__).resolve().parents[2]
RATE_LIMITS = [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000}]
NET = D("0.00015984")     # 0.00016 매수 − 수수료 0.1%(기초 자산) = 순 수령


def _bn():  # type: ignore[no-untyped-def]
    ex = parse_exchange_info({"timezone": "UTC", "serverTime": 1_760_000_040_000, "rateLimits": RATE_LIMITS,
                              "exchangeFilters": [], "symbols": [_symbol()]})
    return ex.symbols["BTCUSDT"]


def _dom():  # type: ignore[no-untyped-def]
    return to_domain_rules(_bn())


# --- V-282 수량 단위 (L1) ------------------------------------------------------------------------

def test_v282_market_qty_step_is_lot_step_when_market_step_zero_same_as_adapter() -> None:
    bn, dom = _bn(), _dom()
    assert dom.market_step == 0 and dom.lot_step == D("0.00001")
    assert market_qty_step(dom) == bn.market_qty_step() == D("0.00001")
    assert market_qty_step(replace(dom, market_step=D("0.0001"))) == D("0.0001")


@pytest.mark.parametrize("stale", [False, True])
def test_v282_sellable_quantity_floors_net_receipt_to_lot_step(stale: bool) -> None:
    assert sellable_quantity(NET, _dom(), stale=stale) == D("0.00015")


def test_v282_virtual_market_step_floors_to_market_step() -> None:
    assert sellable_quantity(NET, replace(_dom(), market_step=D("0.0001")), stale=False) == D("0.0001")


def test_v282_dust_verdict_sends_floored_qty_and_remainder_is_dust() -> None:
    dom = _dom()
    c = dust_verdict(NET, dom, stale=False, market_prices=(D("100000"),), stale_price=None)
    assert (c.verdict, c.quantity) == (DustVerdict.SEND, D("0.00015"))
    rest = dust_verdict(NET - D("0.00015"), dom, stale=False, market_prices=(D("100000"),), stale_price=None)
    assert rest.verdict is DustVerdict.DUST_QUANTITY and rest.quantity == 0


def test_v282_g10_market_sell_unit_check_uses_same_rule() -> None:
    dom = _dom()
    assert filter_violation(dom, Side.SELL, OrderType.MARKET, D("0.00015"), D("100000"), (D("100000"),)) is None
    bad = filter_violation(dom, Side.SELL, OrderType.MARKET, NET, D("100000"), (D("100000"),))
    assert bad is not None and "MARKET_LOT_SIZE" in bad


def test_v282_adapter_order_string_accepts_floored_qty() -> None:
    bn = _bn()
    qty = sellable_quantity(NET, _dom(), stale=False)
    params = new_order_params(NewOrder("BTCUSDT", OrderSide.SELL, OrderKind.MARKET, qty, None, client_order_id("k-1")),
                              qty_step=bn.market_qty_step(), tick=bn.price_filter.tick_size)
    assert ("quantity", "0.00015") in params and ("type", "MARKET") in params


def test_v282_m2_backtest_uses_same_rule() -> None:
    dom = _dom()
    assert backtest_sellable(NET, dom) == sellable_quantity(NET, dom, stale=False) == D("0.00015")


# --- V-282 엔진 경로 (L2): 판정 입력 보유 = 청산 가능 수량, 매도 0.00015, 잔량 dust → 소유권 해제 → 다음 날 진입 ---------

def test_v282_engine_net_receipt_sold_in_lot_units_remainder_dust_then_reentry(tmp_path: Path) -> None:
    w = M2World(tmp_path, qty="0.00016", symbol_rule=replace(_dom(), instrument=SYM), price="100000.00")
    w.set_baseline()
    try:
        w.start_and_resume()
        w.tick_at(1, 0)
        w.tick_at(1, 2)
        assert w.repo.position(ACCOUNT, SYM).quantity == NET
        w.tick_at(4, 0)
        sell = w.sent()[-1]
        assert (sell.side, sell.order_type, sell.quantity) == ("SELL", OrderType.MARKET, D("0.00015"))
        w.tick_at(4, 2)
        assert w.repo.position(ACCOUNT, SYM).quantity == D("0.00000984")
        assert w.repo.active_ownership(ACCOUNT, SYM) is None and len(w.audits("DUST_CONFIRMED")) == 1
        w.tick_at(0, 0, day=DAY + timedelta(days=1))
        w.tick_at(1, 0, day=DAY + timedelta(days=1))
        assert [r.side for r in w.sent()] == ["BUY", "SELL", "BUY"]
        assert w.repo.active_halts(ACCOUNT) == []
    finally:
        w.close()


# --- V-283 시작 순서 (a)(c)(d): 시작 거부, 종료 코드 2, 주문 0 ------------------------------------------------------

PARAMS = 'strategy = "t-m2rules"\nversion = 1\n[params]\nqty = "0.00010"\nqty_step = "{step}"\nnotional = "{notional}"\n'
STRATEGY = ('[[strategies]]\nmodule = "tests.support.m2_engine_world:RulesCheckedStrategy"\n'
            'params = "config/strategies/t-m2rules.toml"\neval_times = ["01:00"]\n')


def _root(tmp_path: Path, step: str = "0.00001", notional: str = "10") -> Path:
    root = make_m2_root(tmp_path, Mode.BACKTEST)
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    (root / "config" / "strategies").mkdir()
    (root / "config" / "strategies" / "t-m2rules.toml").write_text(PARAMS.format(step=step, notional=notional),
                                                                   encoding="utf-8")
    app = root / "config" / "m2" / "app.backtest.toml"
    app.write_text(app.read_text(encoding="utf-8") + STRATEGY, encoding="utf-8")
    return root


def test_v283a_exchange_rules_load_failure_refuses_start(tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
                                                         capsys: pytest.CaptureFixture[str]) -> None:
    root = make_m2_root(tmp_path, Mode.BACKTEST)
    (root / "docs").mkdir()
    shutil.copy(REPO / "docs" / "capabilities.md", root / "docs" / "capabilities.md")
    monkeypatch.setattr(app_main, "_m2_exchange_rules", lambda mode, symbols: ({}, "합성: exchangeInfo 응답 형식 오류"))
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부" in out and "거래 규칙" in out and "exchangeInfo" in out
    assert not (root / "var" / "backtest" / "trading.db").exists()


@pytest.mark.parametrize("step,notional,needle", [("0.0001", "10", "qty_step"), ("0.00001", "9", "notional")])
def test_v283c_validate_rules_mismatch_refuses_start(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                                     step: str, notional: str, needle: str) -> None:
    root = _root(tmp_path, step, notional)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    out = capsys.readouterr().out
    assert "시작 거부" in out and "규칙 불일치" in out and needle in out and "TSTAUSDT" in out
    assert not (root / "var" / "backtest" / "trading.db").exists()


def test_v283d_rules_ok_but_registration_refused_no_orders(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """규칙 검사 통과 → 등록부(현 capabilities.md M2 열 미지원)가 거부 → 시작 거부, 의도·주문 0."""
    root = _root(tmp_path)
    assert run_main(root, {"TRADE_MODE": "BACKTEST"}, max_seconds=0.1, market="M2") == 2
    out = capsys.readouterr().out
    assert "전략 등록 거부" in out and "규칙 불일치" not in out
    db = root / "var" / "backtest" / "trading.db"
    if db.exists():
        repo = Repository(db, Mode.BACKTEST)
        try:
            assert repo.select("SELECT COUNT(*) AS n FROM intent")[0]["n"] == 0
        finally:
            repo.close()
