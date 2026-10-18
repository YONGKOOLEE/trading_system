"""수수료 모델 필드 (CP-040 D-267, T-065). V-336 설정 해석 부분 + 바이낸스 체결의 isMaker 기록 칸.

- `[costs]` maker_rate·taker_rate·rate_source. 기존 commission_rate는 maker·taker가 **둘 다 없을 때만** 읽어
  maker = taker = 그 값(출처 "placeholder" — 현 설정 파일의 "미확정 자리 값" 그대로)으로 본다.
- 셋 동시(commission_rate와 새 키 혼용)·음수·1 이상·부동소수·알 수 없는 출처는 시작 거부(ConfigError).
- 추정 요율 = max(maker, taker) → M2MarketSettings.commission_rate(기존 사용처 = 비용 추정). 체결 요율 = taker.
- isMaker 출처: executionReport `m`, myTrades `isMaker`(docs/broker-specs/binance-spot.md 행 164·171). FULL 응답 fills에는
  maker 필드가 없다(같은 문서 행 121) → None.
모든 값은 가상이다.
"""

from __future__ import annotations

from decimal import Decimal
from pathlib import Path

import pytest

from trading_system.broker.binance.stream import FillLedger, Recorded, parse_my_trades, parse_order_response, \
    parse_event
from trading_system.config.errors import ConfigError
from trading_system.config.m2_files import load_m2_market, parse_m2_market
from trading_system.domain.fees import RATE_SOURCES, FeeModel

ROOT = Path(__file__).resolve().parents[2]
D = Decimal


def market(costs: dict) -> dict:
    return {"gate": {"quote_max_age_seconds": 60, "buy_buffer": "0.005", "sell_buffer": "0.005",
                     "server_time_warn_ms": 500},
            "costs": costs,
            "alerts": {"reject_surge_count": 10, "reject_surge_minutes": 5}}


# --- V-336 설정 해석 ----------------------------------------------------------------------------------------------

def test_v336_maker_taker_estimate_is_max_and_fill_is_taker() -> None:
    s = parse_m2_market(market({"maker_rate": "0.0008", "taker_rate": "0.001", "rate_source": "user"}))
    assert (s.fees.maker_rate, s.fees.taker_rate, s.fees.rate_source) == (D("0.0008"), D("0.001"), "user")
    assert s.commission_rate == D("0.001") == s.fees.estimate_rate          # 추정 = max
    assert s.fees.fill_rate == D("0.001")                                    # 체결 = taker
    s2 = parse_m2_market(market({"maker_rate": "0.002", "taker_rate": "0.001", "rate_source": "account-commission"}))
    assert s2.commission_rate == D("0.002") and s2.fees.fill_rate == D("0.001")   # maker가 커도 추정은 max


def test_v336_commission_rate_only_reads_as_before() -> None:
    s = parse_m2_market(market({"commission_rate": "0.001"}))
    assert s.commission_rate == D("0.001")
    assert (s.fees.maker_rate, s.fees.taker_rate, s.fees.rate_source) == (D("0.001"), D("0.001"), "placeholder")
    s2 = parse_m2_market(market({"commission_rate": "0.001", "rate_source": "user"}))
    assert s2.fees.rate_source == "user"


def test_v336_repo_m2_config_file_behaviour_unchanged() -> None:
    s = load_m2_market(ROOT / "config" / "m2" / "market.paper.toml")
    assert s.commission_rate == D("0.001")                                   # 기존 값 그대로
    assert s.fees == FeeModel(D("0.001"), D("0.001"), "placeholder")


@pytest.mark.parametrize("costs,why", [
    ({"commission_rate": "0.001", "maker_rate": "0.001", "taker_rate": "0.001", "rate_source": "user"}, "함께"),
    ({"commission_rate": "0.001", "maker_rate": "0.001", "taker_rate": "0.001"}, "함께"),
    ({"commission_rate": "0.001", "taker_rate": "0.001", "rate_source": "user"}, "함께"),
    ({"maker_rate": "0.001", "rate_source": "user"}, "둘 다"),
    ({"taker_rate": "0.001", "rate_source": "user"}, "둘 다"),
    ({"maker_rate": "-0.001", "taker_rate": "0.001", "rate_source": "user"}, "maker_rate: 범위"),
    ({"maker_rate": "0.001", "taker_rate": "-0.0001", "rate_source": "user"}, "taker_rate: 범위"),
    ({"maker_rate": "0.001", "taker_rate": "1", "rate_source": "user"}, "taker_rate: 범위"),
    ({"maker_rate": "1.5", "taker_rate": "0.001", "rate_source": "user"}, "maker_rate: 범위"),
    ({"maker_rate": 0.001, "taker_rate": "0.001", "rate_source": "user"}, "maker_rate: 문자열"),
    ({"commission_rate": "-0.001"}, "commission_rate: 범위"),
    ({"commission_rate": "1"}, "commission_rate: 범위"),
    ({"maker_rate": "0.001", "taker_rate": "0.001"}, "rate_source"),
    ({"maker_rate": "0.001", "taker_rate": "0.001", "rate_source": "bnb"}, "rate_source"),
    ({"commission_rate": "0.001", "rate_source": 1}, "rate_source"),
    ({}, "commission_rate"),
    ({"maker_rate": "0.001", "taker_rate": "0.001", "rate_source": "user", "bnb_discount": "0.25"}, "알 수 없는"),
])
def test_v336_refused(costs: dict, why: str) -> None:
    with pytest.raises(ConfigError, match=why):
        parse_m2_market(market(costs))


def test_fee_model_validates_and_sources() -> None:
    assert RATE_SOURCES == ("placeholder", "account-commission", "user")
    m = FeeModel(D("0.0002"), D("0.0004"), "placeholder")
    assert (m.estimate_rate, m.fill_rate, m.is_placeholder) == (D("0.0004"), D("0.0004"), True)
    assert FeeModel(D("0"), D("0"), "user").is_placeholder is False
    for bad in [(D("-0.1"), D("0.001"), "user"), (D("0.001"), D("1"), "user"), (D("0.001"), D("0.001"), "x"),
                (0.001, D("0.001"), "user")]:
        with pytest.raises(ValueError):
            FeeModel(*bad)  # type: ignore[arg-type]


# --- PAPER isMaker 기록 칸 (D-267 (4)) ----------------------------------------------------------------------------

def _trade_report(**over: object) -> dict:
    ev = {"e": "executionReport", "E": 1499405658658, "s": "ETHBTC", "c": "cid1", "S": "BUY", "o": "LIMIT",
          "f": "IOC", "q": "1.00000000", "p": "0.10264410", "x": "TRADE", "X": "FILLED", "r": "NONE", "i": 4293153,
          "l": "1.00000000", "z": "1.00000000", "L": "0.10264410", "n": "0.00100000", "N": "ETH", "T": 1499405658657,
          "t": 717, "Z": "0.10264410", "Y": "0.10264410", "m": False}
    ev.update(over)
    return ev


def test_execution_report_m_recorded_as_is_maker() -> None:
    assert parse_event(_trade_report()).fill.is_maker is False          # type: ignore[union-attr]
    assert parse_event(_trade_report(m=True)).fill.is_maker is True     # type: ignore[union-attr]
    ev = _trade_report()
    del ev["m"]
    assert parse_event(ev).fill.is_maker is None                        # type: ignore[union-attr]
    assert parse_event(_trade_report(m="false")).fill.is_maker is None  # type: ignore[union-attr]


def test_my_trades_is_maker_and_full_fills_none() -> None:
    row = {"symbol": "ETHBTC", "id": 28457, "orderId": 100234, "price": "4.00000100", "qty": "12.00000000",
           "quoteQty": "48.000012", "commission": "10.10000000", "commissionAsset": "BNB", "time": 1499865549590,
           "isBuyer": True, "isMaker": False, "isBestMatch": True}
    assert parse_my_trades([row])[0].is_maker is False
    assert parse_my_trades([{**row, "isMaker": True}])[0].is_maker is True
    full = {"symbol": "ETHBTC", "orderId": 1, "clientOrderId": "c", "status": "FILLED", "type": "LIMIT",
            "side": "BUY", "price": "1", "origQty": "1", "executedQty": "1", "cummulativeQuoteQty": "1",
            "fills": [{"price": "1", "qty": "1", "commission": "0.001", "commissionAsset": "ETH", "tradeId": 5}]}
    assert parse_order_response(full).fills[0].is_maker is None


def test_is_maker_difference_is_not_a_fill_conflict() -> None:
    """FULL 응답(maker 필드 없음 → None) 뒤 같은 체결의 executionReport(m = false)가 와도 값 충돌(K4)이 아니다."""
    full = {"symbol": "ETHBTC", "orderId": 4293153, "clientOrderId": "cid1", "status": "FILLED", "type": "LIMIT",
            "side": "BUY", "price": "0.10264410", "origQty": "1", "executedQty": "1", "cummulativeQuoteQty": "-1",
            "fills": [{"price": "0.10264410", "qty": "1.00000000", "commission": "0.00100000",
                       "commissionAsset": "ETH", "tradeId": 717}]}
    ledger = FillLedger()
    assert ledger.record(parse_order_response(full).fills[0]) is Recorded.NEW
    confirmed = parse_event(_trade_report()).fill                        # type: ignore[union-attr]
    assert ledger.record(confirmed) is Recorded.CONFIRMED                       # type: ignore[arg-type]
    assert ledger.get(confirmed.key).is_maker is False                          # type: ignore[union-attr]
