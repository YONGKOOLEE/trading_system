"""T-M2-04 dust 판정·청산 가능 수량·정밀도 (CP-024 D-102·D-118·D-120, V-131·V-132·V-156·V-160 판정 부분).

순수 함수(domain/trading_rules.py) 시험. 규칙 값은 가상이다(바이낸스 실제 규칙값은 미확인 B-U4).
- 판정은 거래소 규칙 그대로(완충 차감 없음), 실제로 보낼 매도 경로의 필터로 한다:
  평상시 = 시장가(MARKET_LOT_SIZE, 시장가 명목가), 시세 지연 중 = IOC 지정가(LOT_SIZE, 주문가 기준 명목가, D-120).
- (가) 청산 가능 수량(보유를 stepSize로 내림) < minQty → dust 확정.
- (나) 금액: max(최근가, avgPrice, 참조가) 기준으로도 minNotional 미만이면 dust 확정. 그 밖(경계 포함)은 전송 대상
  (전송 뒤 -1013이면 dust 확정 — 시나리오 시험).
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal

import pytest

from trading_system.broker.binance.numbers import format_exact
from trading_system.domain.market import CostRates, estimate_cost
from trading_system.domain.trading_rules import (
    DustVerdict, SymbolRules, dust_verdict, ioc_buy_price, sellable_quantity, stale_sell_price,
)

D = Decimal


def rules(**over: object) -> SymbolRules:
    base: dict[str, object] = dict(
        instrument="TSTA-USDT", tick_size=D("0.01"), min_price=D("0.01"), max_price=D("1000000"),
        lot_min_qty=D("0.0010"), lot_max_qty=D("9000"), lot_step=D("0.0001"),
        market_min_qty=D("0.0010"), market_max_qty=D("9000"), market_step=D("0.0001"),
        min_notional=D("5"), max_notional=None, apply_min_to_market=True, apply_max_to_market=False)
    base.update(over)
    return SymbolRules(**base)  # type: ignore[arg-type]


# --- 청산 가능 수량: 보유를 매도 경로의 stepSize로 내림 (D-102 (가), D-118 수량 내림) ---------------------------

def test_sellable_quantity_floors_to_market_step_normally_and_lot_step_when_stale() -> None:
    r = rules(market_step=D("0.001"), lot_step=D("0.0001"))
    assert sellable_quantity(D("0.12345"), r, stale=False) == D("0.123")
    assert sellable_quantity(D("0.12345"), r, stale=True) == D("0.1234")


def test_sellable_quantity_market_step_zero_means_no_flooring() -> None:
    """MARKET_LOT_SIZE stepSize 0의 의미는 미확인 — 위험 축소라 미리 깎지 않는다(D-106 보수 방향 = 막지 않음)."""
    assert sellable_quantity(D("0.12345"), rules(market_step=D("0")), stale=False) == D("0.12345")


# --- (가) 수량 dust ----------------------------------------------------------------------------------------

@pytest.mark.parametrize(("held", "verdict"), [
    (D("0.0009"), DustVerdict.DUST_QUANTITY),   # minQty 미만
    (D("0.00099"), DustVerdict.DUST_QUANTITY),  # 내림 0.0009 < minQty
    (D("0.0010"), DustVerdict.SEND),            # 경계 = minQty → 전송 (금액은 넉넉하게)
])
def test_quantity_dust_against_market_min_qty(held: Decimal, verdict: DustVerdict) -> None:
    v = dust_verdict(held, rules(), stale=False, market_prices=(D("10000"),), stale_price=None)
    assert v.verdict is verdict


def test_quantity_dust_uses_lot_size_when_stale() -> None:
    """시세 지연 중에는 IOC 지정가로 보낼 것이므로 LOT_SIZE minQty로 판정 (D-120, V-160)."""
    r = rules(market_min_qty=D("0.0100"), lot_min_qty=D("0.0010"))
    normal = dust_verdict(D("0.0050"), r, stale=False, market_prices=(D("10000"),), stale_price=None)
    stale = dust_verdict(D("0.0050"), r, stale=True, market_prices=(), stale_price=D("9950.00"))
    assert normal.verdict is DustVerdict.DUST_QUANTITY
    assert stale.verdict is DustVerdict.SEND


# --- (나) 금액 dust: 확실한 미만만 전송 생략, 경계는 전송 -------------------------------------------------------

def test_notional_dust_only_when_max_reference_price_below_min() -> None:
    r = rules()
    # 0.0010 × max(4000, 4999, 4500) = 4.999 < 5 → 확실한 미만
    v = dust_verdict(D("0.0010"), r, stale=False, market_prices=(D("4000"), D("4999"), D("4500")), stale_price=None)
    assert v.verdict is DustVerdict.DUST_NOTIONAL


def test_notional_boundary_is_sent_and_flagged() -> None:
    """참조가 중 하나라도 최소 금액 이상이면 전송(경계 — 거래소 기준가를 우리가 같게 얻는다는 근거 없음, D-102).
    가장 낮은 참조가로는 미만인 '경계' 표시를 둔다 — 전송 뒤 -1013이면 dust 확정에 쓴다."""
    v = dust_verdict(D("0.0010"), rules(), stale=False, market_prices=(D("4000"), D("5000")), stale_price=None)
    assert v.verdict is DustVerdict.SEND and v.notional_boundary is True
    clear = dust_verdict(D("0.0010"), rules(), stale=False, market_prices=(D("6000"), D("7000")), stale_price=None)
    assert clear.verdict is DustVerdict.SEND and clear.notional_boundary is False


def test_unknown_reference_prices_do_not_block_reduction() -> None:
    """참조가를 모르면 금액으로 미리 막지 않는다 — 전송해 거래소 판정(-1013은 명시 거부, D-112)."""
    v = dust_verdict(D("0.0010"), rules(), stale=False, market_prices=(), stale_price=None)
    assert v.verdict is DustVerdict.SEND and v.notional_boundary is True


def test_apply_min_to_market_false_skips_market_notional() -> None:
    v = dust_verdict(D("0.0010"), rules(apply_min_to_market=False), stale=False, market_prices=(D("1"),),
                     stale_price=None)
    assert v.verdict is DustVerdict.SEND


def test_stale_notional_uses_order_price_exactly() -> None:
    """지연 중 IOC 지정가: 명목가 = 주문가 × 수량(거래소와 같은 값) → 미만이면 확실한 dust (D-120)."""
    r = rules()
    price = stale_sell_price(D("4990.00"), r, D("0.005"))   # 4965.05
    v = dust_verdict(D("0.0010"), r, stale=True, market_prices=(), stale_price=price)
    assert v.verdict is DustVerdict.DUST_NOTIONAL
    ok = dust_verdict(D("0.0011"), r, stale=True, market_prices=(), stale_price=price)
    assert ok.verdict is DustVerdict.SEND and ok.quantity == D("0.0011")


def test_stale_without_price_is_not_dust_but_boundary() -> None:
    v = dust_verdict(D("0.0010"), rules(), stale=True, market_prices=(), stale_price=None)
    assert v.verdict is DustVerdict.SEND and v.notional_boundary is True


def test_zero_or_negative_holding_is_dust_quantity() -> None:
    assert dust_verdict(D("0"), rules(), stale=False, market_prices=(), stale_price=None).verdict \
        is DustVerdict.DUST_QUANTITY


# --- V-156 정밀도: 주문 문자열 자릿수 내림(매수 가격·수량), 비용 추정 올림, 지수 표기 없음 ------------------------------

def test_v156_buy_price_and_quantity_round_down_and_render_plain() -> None:
    r = rules(tick_size=D("0.01"), lot_step=D("0.00001"), market_step=D("0.00001"), lot_min_qty=D("0.00001"),
              market_min_qty=D("0.00001"))
    price = ioc_buy_price(D("12345.678"), r, D("0.005"))          # 12407.406.. → 내림 12407.40
    assert price == D("12407.40")
    qty = sellable_quantity(D("0.000019999"), r, stale=False)     # → 0.00001 (내림)
    assert qty == D("0.00001")
    assert format_exact(price, r.tick_size, "price") == "12407.40"
    assert format_exact(qty, r.market_step, "quantity") == "0.00001"   # 지수 표기(1E-5) 아님
    assert "E" not in format_exact(D("1E-5"), r.market_step, "quantity")


def test_v156_cost_estimate_rounds_up_at_quote_precision() -> None:
    rates = CostRates(commission_rate=D("0.001"), sell_tax_rate=D("0"), cost_quantum=D("0.00000001"))
    assert estimate_cost(D("0.000000011"), False, rates) == D("0.00000001")   # 1.1e-11 → 올림
    assert estimate_cost(D("12407.40") * D("0.00001"), False, rates) == D("0.00012408")


def test_v156_stale_sell_price_rounds_up_to_tick() -> None:
    r = replace(rules(), tick_size=D("0.01"))
    assert stale_sell_price(D("10000.00"), r, D("0.005")) == D("9950.00")
    assert stale_sell_price(D("10000.01"), r, D("0.005")) == D("9950.01")   # 9950.00995 → 올림
