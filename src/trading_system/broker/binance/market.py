"""klines·평균가·최근가 → 시세 입력 (CP-024 D-108·D-116·D-120, BN §6·§8, T-M2-11).

- klines 행: [openTime, open, high, low, close, volume, closeTime, quoteVolume, tradeCount, takerBuyBase, takerBuyQuote,
  ignore] (BN §8). 봉은 open time으로 식별한다. 시각은 UTC(D-040).
- 간격은 길이가 고정인 것만(1s~1w). 1M(월)은 길이가 달라 지원하지 않는다.
- 데이터 품질 실패(KlineDataError → 호출자가 K10 판단): 필드 부족·형식 오류·지수 표기, 시작 시각이 간격 경계가 아님,
  closeTime ≠ openTime + 간격 − 1ms, 순서 역전·중복, high < max(open, close, low), low > min(open, close), 음수 거래량.
  [추정] 간격 경계 = UTC epoch 기준 배수(timeZone 파라미터 기본 0, BN §8). 1w 봉의 경계는 문서 미대조 → 1w는 경계 검사 생략.
- 확정 봉(D-108): 봉 시작 + 간격 ≤ 판정 시각인 봉만. 진행 중 봉은 쓰지 않는다(룩어헤드 방지).
- 최근가: `GET /api/v3/ticker/price` {"symbol","price"} — 응답에 시각이 없으므로 수신 시각(주입 시계)을 시세 시각으로 쓴다.
  평균가: `GET /api/v3/avgPrice` {"mins","price","closeTime"} (BN §6). 참조가(`referencePrice`)는 응답 형식을 원문 대조하지
  않아 이번 작업에서 파서를 만들지 않는다 [미확인 — D-102·D-106 시장가 명목가 판정 전 확인 필요].
- 신선도(D-116, 60초 [결정 A-064]): 시세가 없거나, 나이 > 기준이거나, 판정 시각보다 1초 넘게 미래면(시계 이상) 신선하지 않다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .numbers import BinanceParseError, parse_int, parse_non_negative
from .stream import ms_to_utc

INTERVAL_MS = {"1s": 1_000, "1m": 60_000, "3m": 180_000, "5m": 300_000, "15m": 900_000, "30m": 1_800_000,
               "1h": 3_600_000, "2h": 7_200_000, "4h": 14_400_000, "6h": 21_600_000, "8h": 28_800_000,
               "12h": 43_200_000, "1d": 86_400_000, "3d": 259_200_000, "1w": 604_800_000}
_NO_ALIGN_CHECK = {"3d", "1w"}
FUTURE_TOLERANCE = timedelta(seconds=1)


class KlineDataError(ValueError):
    """시세 데이터 품질 실패 → 호출자가 K10 판단."""


@dataclass(frozen=True)
class Kline:
    symbol: str
    interval: str
    open_time: datetime
    end: datetime              # 봉 시작 + 간격 (이 시각부터 확정)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    quote_volume: Decimal
    trades: int


@dataclass(frozen=True)
class Quote:
    symbol: str
    price: Decimal
    as_of: datetime
    source: str                # "ticker" / "avgPrice"


@dataclass(frozen=True)
class AvgPrice:
    symbol: str
    mins: int
    price: Decimal
    as_of: datetime


def _interval_ms(interval: str) -> int:
    ms = INTERVAL_MS.get(interval)
    if ms is None:
        raise ValueError(f"지원하지 않는 간격: {interval}")
    return ms


def parse_klines(rows: object, *, symbol: str, interval: str) -> list[Kline]:
    step = _interval_ms(interval)
    if not isinstance(rows, list):
        raise KlineDataError("klines가 목록이 아님")
    out: list[Kline] = []
    prev: int | None = None
    for i, row in enumerate(rows):
        try:
            if not isinstance(row, list) or len(row) < 9:
                raise BinanceParseError("필드 부족")
            open_ms, close_ms = parse_int(row[0], "openTime"), parse_int(row[6], "closeTime")
            o, h, lo, c, v = (parse_non_negative(row[k], n) for k, n in
                              ((1, "open"), (2, "high"), (3, "low"), (4, "close"), (5, "volume")))
            qv, trades = parse_non_negative(row[7], "quoteVolume"), parse_int(row[8], "tradeCount")
        except BinanceParseError as exc:
            raise KlineDataError(f"{symbol} {interval} {i}번째 행: {exc}") from None
        if interval not in _NO_ALIGN_CHECK and open_ms % step != 0:
            raise KlineDataError(f"{symbol} {interval} {i}번째 행: 시작 시각이 간격 경계가 아님")
        if close_ms != open_ms + step - 1:
            raise KlineDataError(f"{symbol} {interval} {i}번째 행: closeTime 불일치")
        if prev is not None and open_ms <= prev:
            raise KlineDataError(f"{symbol} {interval} {i}번째 행: 순서 역전·중복")
        if h < max(o, c, lo) or lo > min(o, c) or trades < 0:
            raise KlineDataError(f"{symbol} {interval} {i}번째 행: OHLC 불일치")
        prev = open_ms
        out.append(Kline(symbol, interval, ms_to_utc(open_ms), ms_to_utc(open_ms + step), o, h, lo, c, v, qv, trades))
    return out


def _aware(t: datetime) -> datetime:
    if t.tzinfo is None or t.utcoffset() is None:
        raise ValueError("시간대 없는 시각은 쓰지 않는다")
    return t


def confirmed(bars: list[Kline], now: datetime) -> list[Kline]:
    """판정 시각 now에 확정된 봉만 (봉 시작 + 간격 ≤ now, D-108)."""
    _aware(now)
    return [b for b in bars if b.end <= now]


def parse_avg_price(body: object, *, symbol: str) -> AvgPrice:
    if not isinstance(body, dict):
        raise KlineDataError("avgPrice 응답 형식 아님")
    try:
        mins, price = parse_int(body.get("mins"), "mins"), parse_non_negative(body.get("price"), "price")
        close = parse_int(body.get("closeTime"), "closeTime")
    except BinanceParseError as exc:
        raise KlineDataError(f"avgPrice: {exc}") from None
    if price <= 0:
        raise KlineDataError("avgPrice: 가격 0")
    return AvgPrice(symbol, mins, price, ms_to_utc(close))


def parse_ticker_price(body: object, *, symbol: str, received_at: datetime) -> Quote:
    _aware(received_at)
    if not isinstance(body, dict) or body.get("symbol") != symbol:
        raise KlineDataError("ticker/price: 형식 아님·심볼 불일치")
    try:
        price = parse_non_negative(body.get("price"), "price")
    except BinanceParseError as exc:
        raise KlineDataError(f"ticker/price: {exc}") from None
    if price <= 0:
        raise KlineDataError("ticker/price: 가격 0")
    return Quote(symbol, price, received_at, "ticker")


def is_fresh(quote: Quote | None, now: datetime, *, max_age: timedelta) -> bool:
    _aware(now)
    if quote is None:
        return False
    age = now - quote.as_of
    return -FUTURE_TOLERANCE <= age <= max_age
