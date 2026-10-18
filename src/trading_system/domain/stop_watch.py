"""손절 감시 순수 규칙 (CP-040 D-262·D-263, T-067, 승인 A-115 Q-2 (나)). 표준 라이브러리만.

- 능력 CAP-HLD-02 "보유 손절가 감시": 선언 전략이 소유한 열린 포지션을 시스템이 감시한다(CAP-HLD-01처럼 시스템이 수행하는
  보유 규칙). 전략은 진입·증액 신호에 손절가(00 §2.5)를 싣는다.
- 발동: **닫힌** 1분 봉 종가 ≤ 손절가 (진행 중 분은 보지 않는다 — D-220 닫힌 봉 규칙). 같으면 발동.
- 손절가: 이 소유 기간(소유권을 만든 진입 의도부터)의 체결된(체결 수량 > 0) 진입·증액 의도 가운데 손절가가 있는 가장 최근 것
  (D-263 (1) "증액이면 최신 증액 신호 값" [제안] — 손절가 없는 증액은 건너뜀: 임시 판단).
- 신호 ID(멱등키) = 소유권 ID·감지 분(봉 시작 UTC) — 같은 분은 재시작해도 같은 키(게이트 G4 중복), 다음 분은 새 판단.
- 백테스트 체결(다음 분 시가)은 T-068 몫 — 같은 발동 규칙(stop_triggered)을 쓴다.
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import UTC, datetime
from decimal import Decimal

from .clock import require_aware

STOP_CAPABILITY = "CAP-HLD-02"
STOP_RULE_ID = "CAP-HLD-02"          # 게이트 요청·판정 기록의 규칙 ID (청산 사유·거래 이력) = 원천 손절 감시
STOP_ORIGIN = "STOP_MONITOR"         # 감사 STOP_MONITOR의 원천 표시
STOP_KEY_PREFIX = "stop|"


def stop_triggered(close: Decimal, stop_price: Decimal) -> bool:
    """닫힌 1분 봉 종가가 손절가 이하인지 (D-262). Decimal 값 비교 — 자릿수 표기와 무관."""
    if not isinstance(close, Decimal) or not isinstance(stop_price, Decimal):
        raise TypeError("종가·손절가는 Decimal")
    return close <= stop_price


def stop_idem_key(ownership_id: str, bar_start: datetime) -> str:
    """손절 청산 요청의 멱등키·신호 ID (D-262 "신호 ID = 포지션·감지 분")."""
    require_aware(bar_start)
    if not ownership_id:
        raise ValueError("소유권 ID가 비어 있다")
    return f"{STOP_KEY_PREFIX}{ownership_id}|{bar_start.astimezone(UTC).isoformat()}"


def latest_stop_price(entries: Iterable[tuple[Decimal, Decimal | None]]) -> Decimal | None:
    """(체결 수량, 손절가) — 최신 순. 체결 수량 > 0이고 손절가가 있는 첫 값. 없으면 None (감시 불가 → 청산하지 않고 경고)."""
    for filled, stop in entries:
        if filled > 0 and stop is not None and stop > 0:
            return stop
    return None
