"""klines·평균가·최근가 → 시세 입력 (CP-024 D-108·D-116, V-142(입력 부분)·V-154(시세 입력 부분)).

합성 응답은 공식 문서 klines 배열 형식 [openTime, open, high, low, close, volume, closeTime, quoteVolume,
tradeCount, takerBuyBase, takerBuyQuote, ignore], avgPrice {"mins","price","closeTime"}, ticker/price {"symbol","price"}.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

import pytest

from trading_system.broker.binance import market as m

DAY = 86_400_000
D0 = int(datetime(2026, 10, 5, tzinfo=UTC).timestamp() * 1000)


def _k(open_ms: int, close: str = "100.0", interval_ms: int = DAY, **kw: str) -> list:
    row = [open_ms, kw.get("o", "99.0"), kw.get("h", "101.0"), kw.get("l", "98.0"), close, kw.get("v", "12.5"),
           open_ms + interval_ms - 1, "1250.0", 42, "6.0", "600.0", "0"]
    return row


def test_parse_daily_klines() -> None:
    bars = m.parse_klines([_k(D0), _k(D0 + DAY, "101.5", h="102.0")], symbol="BTCUSDT", interval="1d")
    assert [b.open_time for b in bars] == [datetime(2026, 10, 5, tzinfo=UTC), datetime(2026, 10, 6, tzinfo=UTC)]
    b = bars[1]
    assert (b.open, b.high, b.low, b.close, b.volume) == (
        Decimal("99.0"), Decimal("102.0"), Decimal("98.0"), Decimal("101.5"), Decimal("12.5"))
    assert b.end == datetime(2026, 10, 7, tzinfo=UTC) and b.trades == 42 and b.quote_volume == Decimal("1250.0")


def test_confirmed_bars_exclude_in_progress_bar() -> None:
    # D-108: 1d 봉은 "봉 시작 + 1일" 이후에만 확정 (룩어헤드 방지)
    bars = m.parse_klines([_k(D0), _k(D0 + DAY)], symbol="BTCUSDT", interval="1d")
    now = datetime(2026, 10, 6, 23, 59, 59, tzinfo=UTC)
    assert [b.open_time.day for b in m.confirmed(bars, now)] == [5]
    assert [b.open_time.day for b in m.confirmed(bars, now + timedelta(seconds=1))] == [5, 6]


def test_confirmed_requires_aware_time() -> None:
    bars = m.parse_klines([_k(D0)], symbol="BTCUSDT", interval="1d")
    with pytest.raises(ValueError):
        m.confirmed(bars, datetime(2026, 10, 7))


@pytest.mark.parametrize("rows", [
    [_k(D0 + DAY), _k(D0)],                          # 순서 역전
    [_k(D0), _k(D0)],                                # 중복 시작 시각
    [_k(D0 + 3_600_000)],                            # 1d 경계(UTC 00:00) 아님
    [_k(D0, h="97.0")],                              # high < low
    [_k(D0, close="102.0")],                         # close > high
    [_k(D0, v="-1")],                                # 음수 거래량
    [_k(D0, close="1e2")],                           # 지수 표기
    [[D0, "1", "1", "1", "1"]],                      # 필드 부족
    [_k(D0)[:6] + [D0 + 5] + _k(D0)[7:]],            # closeTime이 간격과 안 맞음
])
def test_bad_klines_are_data_quality_failures(rows: list) -> None:
    with pytest.raises(m.KlineDataError):
        m.parse_klines(rows, symbol="BTCUSDT", interval="1d")


def test_unsupported_interval() -> None:
    with pytest.raises(ValueError):
        m.parse_klines([], symbol="BTCUSDT", interval="1M")   # 월봉은 길이가 고정이 아님


def test_minute_interval() -> None:
    bars = m.parse_klines([_k(D0, interval_ms=60_000), _k(D0 + 60_000, interval_ms=60_000)],
                          symbol="BTCUSDT", interval="1m")
    assert bars[1].end == datetime(2026, 10, 5, 0, 2, tzinfo=UTC)


def test_avg_price_and_ticker_quote() -> None:
    avg = m.parse_avg_price({"mins": 5, "price": "9.35751834", "closeTime": D0}, symbol="BTCUSDT")
    assert avg.price == Decimal("9.35751834") and avg.mins == 5 and avg.as_of == datetime(2026, 10, 5, tzinfo=UTC)
    received = datetime(2026, 10, 5, 0, 0, 3, tzinfo=UTC)
    q = m.parse_ticker_price({"symbol": "BTCUSDT", "price": "4.00000200"}, symbol="BTCUSDT", received_at=received)
    assert q.price == Decimal("4.000002") and q.as_of == received


@pytest.mark.parametrize("body", [
    {"symbol": "ETHUSDT", "price": "1"},     # 다른 심볼
    {"symbol": "BTCUSDT", "price": "0"},     # 0 가격
    {"symbol": "BTCUSDT"},
])
def test_bad_ticker(body: dict) -> None:
    with pytest.raises(m.KlineDataError):
        m.parse_ticker_price(body, symbol="BTCUSDT", received_at=datetime(2026, 10, 5, tzinfo=UTC))


def test_freshness_60s() -> None:
    q = m.Quote("BTCUSDT", Decimal("1"), datetime(2026, 10, 5, tzinfo=UTC), "ticker")
    assert m.is_fresh(q, datetime(2026, 10, 5, 0, 1, tzinfo=UTC), max_age=timedelta(seconds=60))
    assert not m.is_fresh(q, datetime(2026, 10, 5, 0, 1, 0, 1, tzinfo=UTC), max_age=timedelta(seconds=60))
    assert not m.is_fresh(None, datetime(2026, 10, 5, tzinfo=UTC), max_age=timedelta(seconds=60))   # 모르면 지연
    # 미래 시각 시세(시계 이상)도 신선으로 보지 않는다
    assert not m.is_fresh(q, datetime(2026, 10, 4, 23, 59, tzinfo=UTC), max_age=timedelta(seconds=60))
