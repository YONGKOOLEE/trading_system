"""일일 기준 금액 (R-18, 01 §2.4, D-156) — 단일 정의 (A-084 ①).

일일 기준 시각에 고정하는 기준 금액 = 예수금(cash) + 보유 평가액(value). 일일 손실(L4)과 일일 요약·보고의 손익 비율이
같은 분모를 쓰도록 여기서만 정의한다. 감사 기록 DAILY_BASE의 summary 형식(키 이름·순서·문자열 값)은 이전 기록과
같아야 옛 행을 그대로 읽는다: {"trade_date", "cash", "value", "at"}. 추가 키가 있는 옛 행도 읽는다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation


@dataclass(frozen=True)
class DailyBase:
    trade_date: str      # KST 거래일 ISO 날짜 (Repository.trade_date_of와 같은 형식)
    cash: Decimal        # 기준 시각의 예수금
    value: Decimal       # 기준 시각의 보유 평가액
    at: datetime         # 고정한 시각 (시간대 있음)

    @property
    def total(self) -> Decimal:
        """기준 금액 = 예수금 + 보유 평가액."""
        return self.cash + self.value

    def to_summary(self) -> dict[str, str]:
        """감사 기록 DAILY_BASE의 summary. 키 순서·값 표현을 바꾸지 않는다 (옛 행과 바이트 동일)."""
        return {"trade_date": self.trade_date, "cash": str(self.cash), "value": str(self.value),
                "at": self.at.isoformat()}

    @classmethod
    def from_summary(cls, rec: Mapping[str, object]) -> DailyBase:
        """summary → DailyBase. 필수 키가 없거나 값이 잘못되면 ValueError. 추가 키는 무시."""
        try:
            return cls(str(rec["trade_date"]), Decimal(str(rec["cash"])), Decimal(str(rec["value"])),
                       datetime.fromisoformat(str(rec["at"])))
        except (KeyError, InvalidOperation, ValueError, TypeError) as exc:
            raise ValueError(f"DAILY_BASE 요약 형식 오류: {type(exc).__name__}") from exc


class DailyBaseCorrupt(Exception):
    """거래일의 DAILY_BASE 기록이 있으나 읽을 수 없음 — "기록 없음"과 구분한다 (A-084 재검토 M-1).

    손상을 기록 없음으로 보면 지금 금액으로 기준을 새로 고정해 그날 앞선 손실이 L4에서 사라진다. 호출자는 모름으로 다룬다."""
