"""분봉 값 객체 (CP-032 D-177, T-028). 표준 라이브러리만.

- start = 봉 시작 시각(시간대 있는 시각). 키움 ka10080 `cntr_tm`이 봉 시작인지 끝인지 공식 문서에 없으므로(K-U31)
  **시작 시각으로 가정**한다(D-177 보수 규칙): 봉 끝 = start + 길이. 실제가 끝 시각이면 한 봉 늦게 쓸 뿐 룩어헤드는 없다.
- 가격·거래량은 Decimal. adjusted = 수정주가 구분(ka10080 요청 `upd_stkpc_tp`, 기본 "1").
- 저장 키 = (instrument, minutes, start, adjusted).
- 확장 열 (CP-040 D-260, T-066): quote_volume·trades·taker_buy_base·taker_buy_quote — 바이낸스 klines 응답 7~10번째 값
  (docs/broker-specs/binance-spot.md BN S6). 선택 필드라 없으면 None(M1 키움 분봉은 언제나 None). 저장은 추가 전용
  `minute_bar_ext` 표(같은 키) — `minute_bar` 열·스키마 번호는 그대로.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from .clock import require_aware


@dataclass(frozen=True)
class MinuteBar:
    instrument: str
    minutes: int          # 봉 길이(분)
    start: datetime       # 봉 시작 시각 (D-177 가정)
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    adjusted: str = "1"   # 수정주가 구분 (upd_stkpc_tp)
    quote_volume: Decimal | None = None      # [D-260] klines 7번째 값 (quoteVolume)
    trades: int | None = None                # [D-260] 8번째 (tradeCount)
    taker_buy_base: Decimal | None = None    # [D-260] 9번째 (takerBuyBase)
    taker_buy_quote: Decimal | None = None   # [D-260] 10번째 (takerBuyQuote)

    def __post_init__(self) -> None:
        require_aware(self.start)
        if self.minutes <= 0:
            raise ValueError("봉 길이는 1분 이상")

    @property
    def has_ext(self) -> bool:
        """확장 열 네 값이 모두 있는지 (D-260 — 하나라도 없으면 확장 열 결측)."""
        return None not in (self.quote_volume, self.trades, self.taker_buy_base, self.taker_buy_quote)

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)
