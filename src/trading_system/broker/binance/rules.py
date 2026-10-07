"""exchangeInfo → 심볼별 거래 규칙·호출 한도·거래 상태 (CP-024 D-106·D-107·D-113, BN §6·§8, T-M2-11).

- 적재 전체 실패(ExchangeInfoError): 최상위 형식 오류, rateLimits·symbols 없음, 심볼 중복. 호출자는 직전 적재 실패로
  보고 위험 확대 거부·위험 축소 허용(D-106).
- 심볼 단위 실패(rejected): 필수 필터(PRICE_FILTER·LOT_SIZE) 없음, 값 해석 불가, 같은 필터 중복 등. 그 심볼은 규칙이
  "없음"이다(rules() → None, state() → None) — 모르면 위험 확대 거부(호출자). 다른 심볼은 그대로 쓴다.
- 해석하는 필터: PRICE_FILTER, LOT_SIZE, MARKET_LOT_SIZE, MIN_NOTIONAL, NOTIONAL, PERCENT_PRICE, PERCENT_PRICE_BY_SIDE,
  MAX_NUM_ORDERS, (거래소) EXCHANGE_MAX_NUM_ORDERS. 그 밖의 필터 이름은 unhandled_filters에 남긴다(무시하지 않고 기록).
- 값 0의 의미: PRICE_FILTER의 min/max/tick 0은 해당 규칙 비활성(BN §6 확인). LOT_SIZE step 0은 근거 없음 → 심볼 거부.
  MARKET_LOT_SIZE step 0의 의미는 문서 미확인 → [제안] `market_qty_step()`은 LOT_SIZE step으로 대체(더 굵은 단위 아님을
  보장하지 않으므로, 시장가 수량은 두 step 모두의 배수가 되도록 호출자가 확인하는 것을 T-M2-03에서 정한다).
- 거래 상태(D-107, 의미 미확인 B-U13 → 보수 매핑): TRADING만 신규·증액·매도 허용. CANCEL_ONLY = 취소만.
  END_OF_DAY·HALT·BREAK·그 밖의 값 = 취소 시도만 + 알림. [제안] isSpotTradingAllowed가 false면 TRADING이어도 HALTED.
- 게이트 사전 검사(G10)는 이 값 객체를 입력으로 T-M2-03에서 만든다. 여기서는 해석만 한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from enum import Enum

from .numbers import BinanceParseError, parse_int, parse_non_negative
from .weights import RateLimitConfigError, RateLimits, parse_rate_limits


class ExchangeInfoError(ValueError):
    pass


class TradingState(Enum):
    OPEN = ("OPEN", True, True, True, False)
    CANCEL_ONLY = ("CANCEL_ONLY", False, False, True, False)
    HALTED = ("HALTED", False, False, True, True)

    def __init__(self, label: str, allows_new: bool, allows_reduce: bool, allows_cancel: bool, alert: bool) -> None:
        self.label = label
        self.allows_new = allows_new          # 신규·증액
        self.allows_reduce = allows_reduce    # 감액·청산(매도 전송)
        self.allows_cancel = allows_cancel
        self.alert = alert


def trading_state(status: str, *, spot_allowed: bool) -> TradingState:
    if not spot_allowed:
        return TradingState.HALTED
    if status == "TRADING":
        return TradingState.OPEN
    if status == "CANCEL_ONLY":
        return TradingState.CANCEL_ONLY
    return TradingState.HALTED


@dataclass(frozen=True)
class PriceFilter:
    min_price: Decimal
    max_price: Decimal
    tick_size: Decimal


@dataclass(frozen=True)
class QtyFilter:
    min_qty: Decimal
    max_qty: Decimal
    step_size: Decimal


@dataclass(frozen=True)
class NotionalFilter:
    min_notional: Decimal
    apply_min_to_market: bool
    max_notional: Decimal
    apply_max_to_market: bool
    avg_price_mins: int


@dataclass(frozen=True)
class MinNotionalFilter:
    min_notional: Decimal
    apply_to_market: bool
    avg_price_mins: int


@dataclass(frozen=True)
class PercentPriceFilter:
    multiplier_up: Decimal
    multiplier_down: Decimal
    avg_price_mins: int


@dataclass(frozen=True)
class PercentPriceBySideFilter:
    bid_up: Decimal
    bid_down: Decimal
    ask_up: Decimal
    ask_down: Decimal
    avg_price_mins: int


@dataclass(frozen=True)
class SymbolRules:
    symbol: str
    status: str
    state: TradingState
    base_asset: str
    quote_asset: str
    base_asset_precision: int
    quote_asset_precision: int
    base_commission_precision: int
    quote_commission_precision: int
    order_types: frozenset[str]
    price_filter: PriceFilter
    lot_size: QtyFilter
    market_lot_size: QtyFilter | None
    min_notional: MinNotionalFilter | None
    notional: NotionalFilter | None
    percent_price: PercentPriceFilter | None
    percent_price_by_side: PercentPriceBySideFilter | None
    max_num_orders: int | None
    unhandled_filters: tuple[str, ...]

    def market_qty_step(self) -> Decimal:
        m = self.market_lot_size
        return m.step_size if m is not None and m.step_size > 0 else self.lot_size.step_size


@dataclass(frozen=True)
class ExchangeRules:
    server_time_ms: int | None
    rate_limits: RateLimits
    symbols: dict[str, SymbolRules]
    rejected: dict[str, str]
    exchange_max_num_orders: int | None

    def rules(self, symbol: str) -> SymbolRules | None:
        return self.symbols.get(symbol)

    def state(self, symbol: str) -> TradingState | None:
        r = self.symbols.get(symbol)
        return r.state if r is not None else None


def _bool(v: object, field: str) -> bool:
    if not isinstance(v, bool):
        raise BinanceParseError(f"{field}: 참/거짓 아님")
    return v


def _str(v: object, field: str) -> str:
    if not isinstance(v, str) or not v:
        raise BinanceParseError(f"{field}: 문자열 아님")
    return v


def _d(f: dict, k: str) -> Decimal:
    return parse_non_negative(f.get(k), k)


def _filters(raw: object) -> dict[str, dict]:
    if not isinstance(raw, list):
        raise BinanceParseError("filters: 목록 아님")
    out: dict[str, dict] = {}
    for f in raw:
        if not isinstance(f, dict):
            raise BinanceParseError("filters: 항목 형식 아님")
        name = _str(f.get("filterType"), "filterType")
        if name in out:
            raise BinanceParseError(f"필터 중복: {name}")
        out[name] = f
    return out


_HANDLED = {"PRICE_FILTER", "LOT_SIZE", "MARKET_LOT_SIZE", "MIN_NOTIONAL", "NOTIONAL", "PERCENT_PRICE",
            "PERCENT_PRICE_BY_SIDE", "MAX_NUM_ORDERS"}


def _qty(f: dict, *, step_required: bool) -> QtyFilter:
    q = QtyFilter(_d(f, "minQty"), _d(f, "maxQty"), _d(f, "stepSize"))
    if step_required and q.step_size <= 0:
        raise BinanceParseError("LOT_SIZE stepSize 0")
    return q


def parse_symbol(raw: object) -> SymbolRules:
    if not isinstance(raw, dict):
        raise BinanceParseError("심볼 항목 형식 아님")
    fs = _filters(raw.get("filters"))
    if "PRICE_FILTER" not in fs or "LOT_SIZE" not in fs:
        raise BinanceParseError("필수 필터(PRICE_FILTER·LOT_SIZE) 없음")
    pf = fs["PRICE_FILTER"]
    ot = raw.get("orderTypes")
    if not isinstance(ot, list) or not all(isinstance(x, str) for x in ot):
        raise BinanceParseError("orderTypes 형식 아님")
    status = _str(raw.get("status"), "status")
    spot = _bool(raw.get("isSpotTradingAllowed"), "isSpotTradingAllowed")
    g = fs.get
    return SymbolRules(
        symbol=_str(raw.get("symbol"), "symbol"), status=status, state=trading_state(status, spot_allowed=spot),
        base_asset=_str(raw.get("baseAsset"), "baseAsset"), quote_asset=_str(raw.get("quoteAsset"), "quoteAsset"),
        base_asset_precision=parse_int(raw.get("baseAssetPrecision"), "baseAssetPrecision"),
        quote_asset_precision=parse_int(raw.get("quoteAssetPrecision"), "quoteAssetPrecision"),
        base_commission_precision=parse_int(raw.get("baseCommissionPrecision"), "baseCommissionPrecision"),
        quote_commission_precision=parse_int(raw.get("quoteCommissionPrecision"), "quoteCommissionPrecision"),
        order_types=frozenset(ot),
        price_filter=PriceFilter(_d(pf, "minPrice"), _d(pf, "maxPrice"), _d(pf, "tickSize")),
        lot_size=_qty(fs["LOT_SIZE"], step_required=True),
        market_lot_size=_qty(m, step_required=False) if (m := g("MARKET_LOT_SIZE")) else None,
        min_notional=MinNotionalFilter(_d(m, "minNotional"), _bool(m.get("applyToMarket"), "applyToMarket"),
                                       parse_int(m.get("avgPriceMins"), "avgPriceMins"))
        if (m := g("MIN_NOTIONAL")) else None,
        notional=NotionalFilter(_d(m, "minNotional"), _bool(m.get("applyMinToMarket"), "applyMinToMarket"),
                                _d(m, "maxNotional"), _bool(m.get("applyMaxToMarket"), "applyMaxToMarket"),
                                parse_int(m.get("avgPriceMins"), "avgPriceMins"))
        if (m := g("NOTIONAL")) else None,
        percent_price=PercentPriceFilter(_d(m, "multiplierUp"), _d(m, "multiplierDown"),
                                         parse_int(m.get("avgPriceMins"), "avgPriceMins"))
        if (m := g("PERCENT_PRICE")) else None,
        percent_price_by_side=PercentPriceBySideFilter(
            _d(m, "bidMultiplierUp"), _d(m, "bidMultiplierDown"), _d(m, "askMultiplierUp"),
            _d(m, "askMultiplierDown"), parse_int(m.get("avgPriceMins"), "avgPriceMins"))
        if (m := g("PERCENT_PRICE_BY_SIDE")) else None,
        max_num_orders=parse_int(m.get("maxNumOrders"), "maxNumOrders") if (m := g("MAX_NUM_ORDERS")) else None,
        unhandled_filters=tuple(sorted(set(fs) - _HANDLED)),
    )


def parse_exchange_info(body: object) -> ExchangeRules:
    if not isinstance(body, dict):
        raise ExchangeInfoError("exchangeInfo가 객체가 아님")
    if "rateLimits" not in body or "symbols" not in body:
        raise ExchangeInfoError("rateLimits·symbols 없음")
    try:
        limits = parse_rate_limits(body["rateLimits"])
    except RateLimitConfigError as exc:
        raise ExchangeInfoError(f"rateLimits: {exc}") from None
    raw_symbols = body["symbols"]
    if not isinstance(raw_symbols, list):
        raise ExchangeInfoError("symbols가 목록이 아님")
    symbols: dict[str, SymbolRules] = {}
    rejected: dict[str, str] = {}
    seen: set[str] = set()
    for raw in raw_symbols:
        name = raw.get("symbol") if isinstance(raw, dict) else None
        if not isinstance(name, str) or not name:
            raise ExchangeInfoError("심볼 이름 없음")
        if name in seen:
            raise ExchangeInfoError(f"심볼 중복: {name}")
        seen.add(name)
        try:
            symbols[name] = parse_symbol(raw)
        except BinanceParseError as exc:
            rejected[name] = str(exc)
    xmax = None
    xf = body.get("exchangeFilters", [])
    if isinstance(xf, list):
        for f in xf:
            if isinstance(f, dict) and f.get("filterType") == "EXCHANGE_MAX_NUM_ORDERS":
                try:
                    xmax = parse_int(f.get("maxNumOrders"), "maxNumOrders")
                except BinanceParseError as exc:
                    raise ExchangeInfoError(str(exc)) from None
    st = body.get("serverTime")
    return ExchangeRules(st if isinstance(st, int) and not isinstance(st, bool) else None, limits, symbols, rejected,
                         xmax)
