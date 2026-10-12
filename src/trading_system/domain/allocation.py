"""배분 규칙 (CP-034 D-199, A-092 Q-9). 표준 라이브러리만.

- 배분 규칙은 전략 종류 `ALLOCATION`의 명세(StrategySpec.kind)로 등록한다. 명세·등록·상태 전환은 전략과 같다.
- 출력은 지정 전략들(`allocation_targets`)의 **신규 진입 허용 여부** 하나다. 감액·청산은 막지 않는다.
  증액도 위험 확대라 신규 진입과 함께 막는다 [제안 — D-199는 "신규 진입"만 명시, 보수적으로 증액 포함].
- 입력은 지수 일봉(CAP-DAT-05d, ka20006 inds_cd 001)뿐이다. 판정일 d에는 일자 < d이고 확정된 값만 넣는다(CP-033 D-189).
- 입력 결측·지연·품질 실패·규칙 오류 = 배분 규칙 판정 실패 = 지정 전략 신규 진입 거부 + 알림 (조용히 멈추지 않게).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from .clock import require_aware
from .strategy import Bar

STRATEGY_KIND = "STRATEGY"
ALLOCATION_KIND = "ALLOCATION"
SPEC_KINDS = frozenset({STRATEGY_KIND, ALLOCATION_KIND})
ALLOCATION_RULE_KIND = "ALLOCATION"        # 배분 규칙 명세의 규칙 종류 (전략 규칙 종류와 섞지 않는다)
INDEX_CAPABILITY = "CAP-DAT-05d"           # 지수 일봉 능력 [CP-034 D-199 — capabilities.md 행은 별도 반영]
INDEX_KOSPI = "001"                        # ka20006 inds_cd 001 KOSPI 종합


@dataclass(frozen=True)
class IndexView:
    """배분 규칙 판정 입력. bars = 일자 < 판정일이고 확정된 지수 일봉(오래된 순). data_ok가 False면 판정하지 않는다."""

    code: str
    as_of: datetime
    bars: tuple[Bar, ...] = ()
    data_ok: bool = False
    issue: str = ""

    def __post_init__(self) -> None:
        require_aware(self.as_of)


@dataclass(frozen=True)
class AllocationDecision:
    allow_entry: bool
    reason: str
    evidence: dict[str, str] = field(default_factory=dict)
