"""exchangeInfo → 거래 규칙·호출 한도·심볼 상태 (CP-024 D-106·D-107·D-113, V-134(입력 부분)·V-135(매핑 부분)).

합성 응답은 공식 문서 exchangeInfo 예시 형식(필터 키 이름은 filters.md 기준, 값은 합성).
"""

from __future__ import annotations

import copy
from decimal import Decimal

import pytest

from trading_system.broker.binance import rules as r


def _symbol(sym: str = "BTCUSDT", status: str = "TRADING", **kw: object) -> dict:
    d: dict = {
        "symbol": sym, "status": status, "baseAsset": sym[:-4], "baseAssetPrecision": 8, "quoteAsset": "USDT",
        "quotePrecision": 8, "quoteAssetPrecision": 8, "baseCommissionPrecision": 8, "quoteCommissionPrecision": 8,
        "orderTypes": ["LIMIT", "LIMIT_MAKER", "MARKET", "STOP_LOSS_LIMIT", "TAKE_PROFIT_LIMIT"],
        "icebergAllowed": True, "ocoAllowed": True, "quoteOrderQtyMarketAllowed": True, "allowTrailingStop": True,
        "cancelReplaceAllowed": True, "amendAllowed": True, "isSpotTradingAllowed": True,
        "isMarginTradingAllowed": False,
        "filters": [
            {"filterType": "PRICE_FILTER", "minPrice": "0.01000000", "maxPrice": "1000000.00000000",
             "tickSize": "0.01000000"},
            {"filterType": "LOT_SIZE", "minQty": "0.00001000", "maxQty": "9000.00000000", "stepSize": "0.00001000"},
            {"filterType": "ICEBERG_PARTS", "limit": 10},
            {"filterType": "MARKET_LOT_SIZE", "minQty": "0.00000000", "maxQty": "100.00000000",
             "stepSize": "0.00000000"},
            {"filterType": "TRAILING_DELTA", "minTrailingAboveDelta": 10, "maxTrailingAboveDelta": 2000,
             "minTrailingBelowDelta": 10, "maxTrailingBelowDelta": 2000},
            {"filterType": "PERCENT_PRICE_BY_SIDE", "bidMultiplierUp": "5", "bidMultiplierDown": "0.2",
             "askMultiplierUp": "5", "askMultiplierDown": "0.2", "avgPriceMins": 5},
            {"filterType": "NOTIONAL", "minNotional": "5.00000000", "applyMinToMarket": True,
             "maxNotional": "9000000.00000000", "applyMaxToMarket": False, "avgPriceMins": 5},
            {"filterType": "MAX_NUM_ORDERS", "maxNumOrders": 200},
            {"filterType": "MAX_NUM_ALGO_ORDERS", "maxNumAlgoOrders": 5},
        ],
        "permissions": [], "permissionSets": [["SPOT"]], "defaultSelfTradePreventionMode": "EXPIRE_MAKER",
        "allowedSelfTradePreventionModes": ["NONE", "EXPIRE_TAKER", "EXPIRE_MAKER", "EXPIRE_BOTH"],
    }
    d.update(kw)
    return d


INFO = {
    "timezone": "UTC", "serverTime": 1_760_000_040_000,
    "rateLimits": [{"rateLimitType": "REQUEST_WEIGHT", "interval": "MINUTE", "intervalNum": 1, "limit": 6000},
                   {"rateLimitType": "ORDERS", "interval": "SECOND", "intervalNum": 10, "limit": 100}],
    "exchangeFilters": [{"filterType": "EXCHANGE_MAX_NUM_ORDERS", "maxNumOrders": 1000}],
    "symbols": [_symbol(), _symbol("ETHUSDT", "CANCEL_ONLY"), _symbol("XRPUSDT", "BREAK")],
}


def test_parse_symbol_rules() -> None:
    ex = r.parse_exchange_info(INFO)
    btc = ex.symbols["BTCUSDT"]
    assert (btc.base_asset, btc.quote_asset) == ("BTC", "USDT")
    assert btc.price_filter == r.PriceFilter(Decimal("0.01"), Decimal("1000000"), Decimal("0.01"))
    assert btc.lot_size == r.QtyFilter(Decimal("0.00001"), Decimal("9000"), Decimal("0.00001"))
    assert btc.market_lot_size == r.QtyFilter(Decimal("0"), Decimal("100"), Decimal("0"))
    assert btc.notional == r.NotionalFilter(Decimal("5"), True, Decimal("9000000"), False, 5)
    assert btc.percent_price_by_side is not None and btc.percent_price_by_side.bid_down == Decimal("0.2")
    assert btc.max_num_orders == 200
    assert "MARKET" in btc.order_types and btc.quote_asset_precision == 8
    assert set(btc.unhandled_filters) == {"ICEBERG_PARTS", "TRAILING_DELTA", "MAX_NUM_ALGO_ORDERS"}
    assert ex.exchange_max_num_orders == 1000
    assert ex.rate_limits.weight[0].limit == 6000
    assert ex.rejected == {}


def test_market_qty_step_falls_back_to_lot_size_when_zero() -> None:
    # MARKET_LOT_SIZE stepSize 0의 의미는 문서 미확인 → 보수적으로 LOT_SIZE step 사용 [제안]
    btc = r.parse_exchange_info(INFO).symbols["BTCUSDT"]
    assert btc.market_qty_step() == Decimal("0.00001")


@pytest.mark.parametrize(("status", "state"), [
    ("TRADING", r.TradingState.OPEN),
    ("CANCEL_ONLY", r.TradingState.CANCEL_ONLY),
    ("END_OF_DAY", r.TradingState.HALTED),
    ("HALT", r.TradingState.HALTED),
    ("BREAK", r.TradingState.HALTED),
    ("SOMETHING_NEW", r.TradingState.HALTED),
])
def test_status_mapping_d107(status: str, state: r.TradingState) -> None:
    st = r.trading_state(status, spot_allowed=True)
    assert st is state
    assert st.allows_new is (status == "TRADING")
    assert st.allows_reduce is (status == "TRADING")
    assert st.allows_cancel is True
    assert st.alert is (state is r.TradingState.HALTED)


def test_spot_not_allowed_is_halted() -> None:
    assert r.trading_state("TRADING", spot_allowed=False) is r.TradingState.HALTED


def test_state_of_unknown_symbol_is_none() -> None:
    ex = r.parse_exchange_info(INFO)
    assert ex.state("XRPUSDT") is r.TradingState.HALTED
    assert ex.state("DOGEUSDT") is None            # 모르면 없음 → 위험 확대 거부(호출자)
    assert ex.rules("DOGEUSDT") is None


@pytest.mark.parametrize("mutate", [
    lambda s: s["filters"].pop(0),                                       # PRICE_FILTER 없음
    lambda s: s["filters"].pop(1),                                       # LOT_SIZE 없음
    lambda s: s["filters"][0].update(tickSize="1e-2"),                   # 지수 표기
    lambda s: s["filters"][1].update(stepSize="0"),                      # LOT_SIZE step 0
    lambda s: s.update(quoteAssetPrecision="8"),
    lambda s: s["filters"].append({"filterType": "PRICE_FILTER", "minPrice": "0", "maxPrice": "0",
                                   "tickSize": "0.1"}),                  # 같은 필터 중복
])
def test_bad_symbol_is_rejected_others_kept(mutate) -> None:  # type: ignore[no-untyped-def]
    info = copy.deepcopy(INFO)
    mutate(info["symbols"][0])
    ex = r.parse_exchange_info(info)
    assert "BTCUSDT" not in ex.symbols and "BTCUSDT" in ex.rejected
    assert "ETHUSDT" in ex.symbols
    assert ex.state("BTCUSDT") is None


def test_missing_rate_limits_or_symbols_fails_whole_load() -> None:
    for key in ("rateLimits", "symbols"):
        info = copy.deepcopy(INFO)
        del info[key]
        with pytest.raises(r.ExchangeInfoError):
            r.parse_exchange_info(info)
    with pytest.raises(r.ExchangeInfoError):
        r.parse_exchange_info([])


def test_duplicate_symbol_fails_whole_load() -> None:
    info = copy.deepcopy(INFO)
    info["symbols"].append(_symbol())
    with pytest.raises(r.ExchangeInfoError):
        r.parse_exchange_info(info)


def test_legacy_min_notional_and_percent_price() -> None:
    sym = _symbol()
    sym["filters"] = [f for f in sym["filters"] if f["filterType"] not in ("NOTIONAL", "PERCENT_PRICE_BY_SIDE")]
    sym["filters"] += [{"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000", "applyToMarket": True,
                        "avgPriceMins": 5},
                       {"filterType": "PERCENT_PRICE", "multiplierUp": "5", "multiplierDown": "0.2",
                        "avgPriceMins": 5}]
    ex = r.parse_exchange_info({**INFO, "symbols": [sym]})
    btc = ex.symbols["BTCUSDT"]
    assert btc.min_notional == r.MinNotionalFilter(Decimal("10"), True, 5)
    assert btc.percent_price == r.PercentPriceFilter(Decimal("5"), Decimal("0.2"), 5)
    assert btc.notional is None
