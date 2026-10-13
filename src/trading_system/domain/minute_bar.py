"""분봉 값 객체 (CP-032 D-177, T-028). 표준 라이브러리만.

- start = 봉 시작 시각(시간대 있는 시각). 키움 ka10080 `cntr_tm`이 봉 시작인지 끝인지 공식 문서에 없으므로(K-U31)
  **시작 시각으로 가정**한다(D-177 보수 규칙): 봉 끝 = start + 길이. 실제가 끝 시각이면 한 봉 늦게 쓸 뿐 룩어헤드는 없다.
- 가격·거래량은 Decimal. adjusted = 수정주가 구분(ka10080 요청 `upd_stkpc_tp`, 기본 "1").
- 저장 키 = (instrument, minutes, start, adjusted).
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

    def __post_init__(self) -> None:
        require_aware(self.start)
        if self.minutes <= 0:
            raise ValueError("봉 길이는 1분 이상")

    @property
    def end(self) -> datetime:
        return self.start + timedelta(minutes=self.minutes)
