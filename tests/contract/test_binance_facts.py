"""바이낸스 규칙·시세·상태 공급자 → 공통 계약 MarketFacts·SymbolRulesSource (CP-024 D-106·D-107·D-108·D-116·D-119,
T-M2-11). V-134(규칙 미적재 → 위험 확대 거부·축소 허용), V-135(심볼 상태 매핑), V-142(진행 중 1d 봉 미사용),
V-154(시세 입력: 시세 시각 = 수신 시각, 신선도 판정 입력).

네트워크 없음. 합성 응답은 공식 문서 필드 정의 형식(exchangeInfo·klines·ticker/price·avgPrice), 값은 합성.
referencePrice 응답 필드명({"symbol","referencePrice","timestamp"})은 원문 미대조 [미확인] — 합성 응답으로만 고정한다.
"""

from __future__ import annotations

import copy
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from typing import Any

import pytest

from tests.contract.test_binance_market import DAY, _k
from tests.contract.test_binance_rules import INFO, _symbol
from trading_system.broker.binance.facts import BinanceMarketFacts, to_domain_rules
from trading_system.broker.binance.rules import parse_exchange_info
from trading_system.domain.enums import HaltType, OrderType, Side
from trading_system.domain.market import InstrumentKind, TradingStatus
from trading_system.domain.trading_rules import SymbolRules as DomainRules
from trading_system.domain.trading_rules import filter_violation

D = Decimal
NOW = datetime(2026, 10, 7, 1, 0, tzinfo=UTC)
D0 = int(datetime(2026, 10, 5, tzinfo=UTC).timestamp() * 1000)
UNIVERSE = ("BTCUSDT", "ETHUSDT", "XRPUSDT")


class Halts:
    """HaltControl 기록용 가짜 (K10 발동·해제 조건 보고)."""

    def __init__(self) -> None:
        self.activated: list[tuple[HaltType, str | None, str | None]] = []
        self.reported: list[tuple[HaltType, str, bool, str | None]] = []

    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None, *,
                 instrument: str | None = None, owner: str | None = None) -> Any:
        self.activated.append((halt_type, account, instrument))

    def report_condition(self, halt_type: HaltType, account: str | None, name: str, ok: bool, evidence: str = "", *,
                         instrument: str | None = None, owner: str | None = None,
                         recon: str | None = None) -> list[str]:
        self.reported.append((halt_type, name, ok, instrument))
        return []

    def resume(self, *a: Any, **k: Any) -> object:   # pragma: no cover — 공급자는 쓰지 않는다
        raise AssertionError


class Clock:
    def __init__(self) -> None:
        self.t = NOW

    def now(self) -> datetime:
        return self.t


def facts(halts: Halts | None = None, clock: Clock | None = None, info: dict | None = None) -> BinanceMarketFacts:
    c = clock or Clock()
    f = BinanceMarketFacts(now=c.now, universe=UNIVERSE, halts=halts, account="BN-BT-01")
    assert f.load_exchange_info(copy.deepcopy(INFO if info is None else info)) is True
    return f


# --- exchangeInfo → 공통 규칙 값 객체 (D-106) ---------------------------------------------------

def test_to_domain_rules_maps_filters() -> None:
    btc = parse_exchange_info(INFO).symbols["BTCUSDT"]
    d = to_domain_rules(btc)
    assert isinstance(d, DomainRules) and d.instrument == "BTCUSDT"
    assert (d.tick_size, d.min_price, d.max_price) == (D("0.01"), D("0.01"), D("1000000"))
    assert (d.lot_min_qty, d.lot_max_qty, d.lot_step) == (D("0.00001"), D("9000"), D("0.00001"))
    # MARKET_LOT_SIZE stepSize 0(의미 미확인)은 그대로 0 = 단위 검사 안 함 (위험 축소, domain 규칙)
    assert (d.market_min_qty, d.market_max_qty, d.market_step) == (D("0"), D("100"), D("0"))
    assert (d.min_notional, d.max_notional, d.apply_min_to_market, d.apply_max_to_market) == \
        (D("5"), D("9000000"), True, False)


def test_to_domain_rules_without_market_lot_size_uses_lot_size() -> None:
    sym = _symbol()
    sym["filters"] = [f for f in sym["filters"] if f["filterType"] != "MARKET_LOT_SIZE"]
    d = to_domain_rules(parse_exchange_info(dict(INFO, symbols=[sym])).symbols["BTCUSDT"])
    assert (d.market_min_qty, d.market_max_qty, d.market_step) == (D("0.00001"), D("9000"), D("0.00001"))


def test_to_domain_rules_min_notional_filter_only() -> None:
    sym = _symbol()
    sym["filters"] = [f for f in sym["filters"] if f["filterType"] != "NOTIONAL"] + [
        {"filterType": "MIN_NOTIONAL", "minNotional": "10.00000000", "applyToMarket": False, "avgPriceMins": 5}]
    d = to_domain_rules(parse_exchange_info(dict(INFO, symbols=[sym])).symbols["BTCUSDT"])
    assert (d.min_notional, d.max_notional, d.apply_min_to_market, d.apply_max_to_market) == \
        (D("10"), None, False, False)


def test_to_domain_rules_no_notional_filter_has_zero_min() -> None:
    sym = _symbol()
    sym["filters"] = [f for f in sym["filters"] if f["filterType"] != "NOTIONAL"]
    d = to_domain_rules(parse_exchange_info(dict(INFO, symbols=[sym])).symbols["BTCUSDT"])
    assert (d.min_notional, d.max_notional) == (D("0"), None)


@pytest.mark.parametrize("ftype,key,value", [("LOT_SIZE", "minQty", "0.000015"),
                                             ("PRICE_FILTER", "minPrice", "0.015")])
def test_min_not_multiple_of_step_means_rules_unknown(ftype: str, key: str, value: str) -> None:
    """BN §6 LOT_SIZE·PRICE_FILTER 식 원문 대조 권장(trading_rules 주석): minQty·minPrice가 단위의 배수가 아니면 [제안]
    그 심볼 규칙을 "없음"으로 본다(위험 확대 거부). 다른 심볼은 그대로."""
    sym = _symbol()
    for f in sym["filters"]:
        if f["filterType"] == ftype:
            f[key] = value
    f = facts(info=dict(INFO, symbols=[sym, _symbol("ETHUSDT")]))
    assert f.rules("BTCUSDT") is None and "BTCUSDT" in f.rejected
    assert f.rules("ETHUSDT") is not None


# --- MarketFacts: 종목·상태·세션·호가 단위 (D-107, D-108, T-027 tick_of) --------------------------

def test_instrument_info_from_rules() -> None:
    info = facts().instrument("BTCUSDT")
    assert info is not None and info.lot_size == D("0.00001") and info.leverage == 1 and not info.is_leveraged
    assert info.kind is InstrumentKind.STOCK    # 임시 — 크립토 상품 유형은 아직 없음(m2_fakes와 같은 처리)


def test_unknown_or_outside_universe_symbol_is_none() -> None:
    f = facts()
    for sym in ("DOGEUSDT", "SOLUSDT"):   # 규칙에 없음 / 유니버스 밖
        assert f.instrument(sym) is None and f.trading_status(sym) is None and f.rules(sym) is None
        assert f.tick_valid(sym, D("1")) is None and f.tick_of(sym, D("1")) is None and f.quote(sym) is None
    info = dict(INFO, symbols=INFO["symbols"] + [_symbol("SOLUSDT")])
    assert facts(info=info).rules("SOLUSDT") is None     # 거래소에는 있으나 유니버스 밖


@pytest.mark.parametrize("sym,expected", [("BTCUSDT", frozenset()),
                                          ("ETHUSDT", frozenset({TradingStatus.HALTED})),    # CANCEL_ONLY = 취소만
                                          ("XRPUSDT", frozenset({TradingStatus.HALTED}))])  # BREAK 등 = 취소만
def test_v135_symbol_status_mapping(sym: str, expected: frozenset) -> None:
    assert facts().trading_status(sym) == expected


def test_spot_trading_not_allowed_is_halted() -> None:
    info = dict(INFO, symbols=[_symbol(isSpotTradingAllowed=False)])
    assert facts(info=info).trading_status("BTCUSDT") == frozenset({TradingStatus.HALTED})


def test_session_is_247_utc_day() -> None:
    s = facts().session(date(2026, 10, 10))   # 토요일도 열림
    assert s is not None and s.is_open
    assert (s.regular_open, s.regular_close) == (datetime(2026, 10, 10, tzinfo=UTC), datetime(2026, 10, 11, tzinfo=UTC))


def test_tick_valid_and_tick_of_from_tick_size() -> None:
    f = facts()
    assert f.tick_valid("BTCUSDT", D("100.01")) is True and f.tick_valid("BTCUSDT", D("100.015")) is False
    assert f.tick_of("BTCUSDT", D("100.01")) == D("0.01") and f.tick_of("BTCUSDT", D("5")) == D("0.01")


def test_tick_size_zero_means_no_tick_rule() -> None:
    """BN §6: tickSize 0 = 규칙 비활성 → 어떤 가격도 단위 위반 아님, 단위 값 자체는 없음(None)."""
    sym = _symbol()
    for flt in sym["filters"]:
        if flt["filterType"] == "PRICE_FILTER":
            flt["tickSize"] = "0.00000000"
            flt["minPrice"] = "0.00000000"
    f = facts(info=dict(INFO, symbols=[sym]))
    assert f.tick_valid("BTCUSDT", D("100.123456")) is True and f.tick_of("BTCUSDT", D("100")) is None


# --- 적재 실패 (V-134) -------------------------------------------------------------------------

def test_v134_load_failure_drops_rules_but_keeps_last_instrument_for_reduction() -> None:
    """직전 적재 실패 → rules·상태 None(위험 확대 거부). 종목 정보·호가 단위는 마지막 정상 값(위험 축소 수량·가격 검사)."""
    f = facts()
    assert f.load_exchange_info({"symbols": []}) is False      # rateLimits 없음 → 전체 실패
    assert f.rules("BTCUSDT") is None and f.trading_status("BTCUSDT") is None
    assert f.rate_limits() is None and f.load_error is not None
    info = f.instrument("BTCUSDT")
    assert info is not None and info.lot_size == D("0.00001")
    assert f.tick_valid("BTCUSDT", D("100.01")) is True
    assert f.load_exchange_info(copy.deepcopy(INFO)) is True and f.rules("BTCUSDT") is not None
    assert f.load_error is None and f.rate_limits() is not None


def test_never_loaded_knows_nothing() -> None:
    f = BinanceMarketFacts(now=Clock().now, universe=UNIVERSE)
    assert f.rules("BTCUSDT") is None and f.instrument("BTCUSDT") is None and f.trading_status("BTCUSDT") is None
    assert f.tick_valid("BTCUSDT", D("1")) is None and f.rate_limits() is None


def test_rate_limits_exposed() -> None:
    rl = facts().rate_limits()
    assert rl is not None and rl.weight[0].limit == 6000


# --- 시세: 최근가·평균가·참조가 (D-102·D-106·D-116) ------------------------------------------------

def test_ticker_quote_time_is_receive_time_and_freshness_input() -> None:
    c = Clock()
    f = facts(clock=c)
    assert f.update_ticker("BTCUSDT", {"symbol": "BTCUSDT", "price": "100.01000000"}) is True
    q = f.quote("BTCUSDT")
    assert q is not None and (q.price, q.at) == (D("100.01"), NOW)
    assert q.upper_limit is None and q.lower_limit is None and q.base_price is None   # 가격제한폭 없음
    c.t = NOW + timedelta(seconds=90)
    assert f.quote("BTCUSDT").at == NOW            # 시각은 받은 시각 그대로 → 게이트가 신선도(60초) 판정


@pytest.mark.parametrize("body", [{"symbol": "ETHUSDT", "price": "1"}, {"symbol": "BTCUSDT", "price": "0"},
                                  {"symbol": "BTCUSDT", "price": "1e2"}, {"symbol": "BTCUSDT"}, ["x"]])
def test_bad_ticker_makes_quote_unknown(body: object) -> None:
    f = facts()
    f.update_ticker("BTCUSDT", {"symbol": "BTCUSDT", "price": "100.00"})
    assert f.update_ticker("BTCUSDT", body) is False
    assert f.quote("BTCUSDT") is None             # 모르면 없음 → 위험 확대 거부 (G11)


def test_ticker_outside_universe_ignored() -> None:
    f = facts()
    assert f.update_ticker("SOLUSDT", {"symbol": "SOLUSDT", "price": "1"}) is False and f.quote("SOLUSDT") is None


def test_market_prices_collects_known_reference_prices() -> None:
    f = facts()
    assert f.market_prices("BTCUSDT") == ()
    f.update_ticker("BTCUSDT", {"symbol": "BTCUSDT", "price": "100.00"})
    assert f.update_avg_price("BTCUSDT", {"mins": 5, "price": "99.50", "closeTime": D0 + DAY}) is True
    assert f.update_reference_price("BTCUSDT", {"symbol": "BTCUSDT", "referencePrice": "101.00",
                                                "timestamp": D0}) is True
    assert sorted(f.market_prices("BTCUSDT")) == [D("99.50"), D("100.00"), D("101.00")]


def test_reference_price_null_or_unset_means_none() -> None:
    """참조가 미설정(null 또는 -2043 수신)은 정상 — "참조가 없음". 형식 오류는 그 값만 모름."""
    f = facts()
    f.update_reference_price("BTCUSDT", {"symbol": "BTCUSDT", "referencePrice": "101.00", "timestamp": D0})
    assert f.update_reference_price("BTCUSDT", {"symbol": "BTCUSDT", "referencePrice": None, "timestamp": D0}) is True
    assert f.market_prices("BTCUSDT") == ()
    f.update_reference_price("BTCUSDT", {"symbol": "BTCUSDT", "referencePrice": "101.00", "timestamp": D0})
    f.clear_reference_price("BTCUSDT")
    assert f.market_prices("BTCUSDT") == ()
    for bad in ({"symbol": "ETHUSDT", "referencePrice": "1", "timestamp": D0},
                {"symbol": "BTCUSDT", "referencePrice": "-1", "timestamp": D0},
                {"symbol": "BTCUSDT", "referencePrice": 1.5, "timestamp": D0}, "x"):
        f.update_reference_price("BTCUSDT", {"symbol": "BTCUSDT", "referencePrice": "101.00", "timestamp": D0})
        assert f.update_reference_price("BTCUSDT", bad) is False
        assert f.market_prices("BTCUSDT") == ()


def test_bad_avg_price_makes_value_unknown() -> None:
    f = facts()
    f.update_avg_price("BTCUSDT", {"mins": 5, "price": "99.50", "closeTime": D0})
    assert f.update_avg_price("BTCUSDT", {"mins": 5, "price": "0", "closeTime": D0}) is False
    assert f.market_prices("BTCUSDT") == ()


def test_market_prices_feed_g10_certain_violation_check() -> None:
    """시장가 매도 명목가: 아는 참조가 모두로도 minNotional 미만일 때만 거부 (D-102·D-106, T-M2-03 규칙에 연결)."""
    f = facts()
    rules = f.rules("BTCUSDT")
    assert rules is not None
    f.update_ticker("BTCUSDT", {"symbol": "BTCUSDT", "price": "100.00"})
    f.update_avg_price("BTCUSDT", {"mins": 5, "price": "200.00", "closeTime": D0})
    # 0.03 × 100 = 3 < 5 이지만 0.03 × 200 = 6 ≥ 5 → 확실한 위반 아님
    assert filter_violation(rules, Side.SELL, OrderType.MARKET, D("0.03"), D("100"), f.market_prices("BTCUSDT")) is None
    assert filter_violation(rules, Side.SELL, OrderType.MARKET, D("0.02"), D("100"),
                            f.market_prices("BTCUSDT")) is not None


# --- klines → 1d 봉 (D-108, V-142), 데이터 품질 실패 → K10 ------------------------------------------

def test_v142_daily_bars_only_confirmed() -> None:
    c = Clock()
    c.t = datetime(2026, 10, 6, 23, 59, 59, tzinfo=UTC)
    h = Halts()
    f = facts(halts=h, clock=c)
    assert f.update_klines("BTCUSDT", [_k(D0), _k(D0 + DAY)]) is True
    assert [b.open_time.day for b in f.daily_bars("BTCUSDT")] == [5]       # 10-06 봉은 진행 중
    c.t += timedelta(seconds=1)
    assert [b.open_time.day for b in f.daily_bars("BTCUSDT")] == [5, 6]
    assert h.reported == [(HaltType.K10, "data_ok", True, "BTCUSDT")] and h.activated == []


@pytest.mark.parametrize("rows", [[_k(D0 + DAY), _k(D0)],                          # 순서 역전
                                  [_k(D0 + 1000)],                                 # 경계 아님
                                  [_k(D0, h="90.0")],                              # OHLC 불일치
                                  "x"])
def test_kline_quality_failure_k10_and_bars_unknown(rows: object) -> None:
    h = Halts()
    f = facts(halts=h)
    f.update_klines("BTCUSDT", [_k(D0)])
    assert f.update_klines("BTCUSDT", rows) is False
    assert f.daily_bars("BTCUSDT") == []
    assert h.activated == [(HaltType.K10, "BN-BT-01", "BTCUSDT")]


def test_klines_outside_universe_ignored_and_no_halt_object_ok() -> None:
    f = facts(halts=None)
    assert f.update_klines("SOLUSDT", [_k(D0)]) is False and f.daily_bars("SOLUSDT") == []
    assert f.update_klines("BTCUSDT", "x") is False      # 정지 연결이 없어도 예외 없이 "모름"


def test_provider_satisfies_gate_ports() -> None:
    """공통 계약 메서드가 모두 있다 (gate.ports.MarketFacts·SymbolRulesSource — 구조적 형식)."""
    f = facts()
    for name in ("quote", "instrument", "trading_status", "session", "tick_valid", "tick_of", "rules",
                 "market_prices"):
        assert callable(getattr(f, name))
