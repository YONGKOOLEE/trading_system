"""정지 유형별 요청 허용 규칙 (01 §2.6 표, D-021, 02 ENT-23·ST-06). 순수 함수.

요청 분류:
- 위험 확대: 신규 진입·증액·매수 정정 → 모든 정지에서 차단 (K9·K10은 범위 안에서만)
- 위험 축소: 감액·청산·매도 정정
- 취소
여러 정지가 걸리면 모든 정지가 허용해야 허용한다 (가장 엄격한 동작, D-021).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from .enums import HaltType, RequestSource, RequestType, Side


class HaltState(StrEnum):
    """02 ST-06."""

    ACTIVE = "ACTIVE"
    AWAITING_RESUME = "AWAITING_RESUME"  # 재개 조건 충족, 사용자 재개 대기
    RELEASED = "RELEASED"


class K9Variant(StrEnum):
    REPLACE = "REPLACE"  # 버전 교체: 해당 전략의 감액·청산 신호 허용
    USER = "USER"        # 사용자 판단: 운영 요청만


# 조건이 충족되면 사람 없이 해제되는 유형 (ST-06)
AUTO_RELEASE = frozenset({HaltType.K5, HaltType.K6, HaltType.K10, HaltType.K11})
# 원격(메신저)으로 재개할 수 있는 유형 (01 §2.9, Q-H)
REMOTE_RESUMABLE = frozenset({HaltType.K1, HaltType.K2})
# 원격 재개 시 명령 처리 중 새로 한 대조 기준으로 조건을 다시 보는 유형 → 그 재개 조건 이름 (CP-026, A-069 ①②).
# 조건의 증거에 이번 대조 ID가 있어야 한다 — 이전 대조의 충족 기록으로는 원격 재개하지 않는다
REMOTE_FRESH_RECONCILE: dict[HaltType, str] = {HaltType.K4: "reconciled", HaltType.K8: "recovery_ok"}
# 자동 운영(PAPER 무인, CP-029 D-152)이 재개할 수 있는 유형 — K8만, 원격 K8과 같은 새 대조 기준(REMOTE_FRESH_RECONCILE)
AUTO_RESUMABLE = frozenset({HaltType.K8})
# 발동 시 매수 미체결을 취소하는 유형 (01 §2.6 "미체결 주문" 열)
CANCELS_OPEN_BUYS = frozenset({HaltType.K1, HaltType.K2, HaltType.K9})
# 발동 시 긴급 알림 유형 (01 §2.10)
EMERGENCY = frozenset({HaltType.K1, HaltType.K2, HaltType.K3, HaltType.K4, HaltType.K5, HaltType.K6,
                       HaltType.K7, HaltType.K8})


@dataclass(frozen=True)
class HaltView:
    halt_type: HaltType
    instrument: str | None = None  # K3: 결과 불명 종목, K10: 품질 문제 종목 (None = 계좌 전체)
    owner: str | None = None       # K9: 중지된 전략
    variant: str | None = None     # K9: REPLACE / USER


@dataclass(frozen=True)
class RequestView:
    source: RequestSource
    request_type: RequestType
    side: Side
    instrument: str
    owner: str | None

    @property
    def increasing(self) -> bool:
        return self.request_type in (RequestType.ENTRY, RequestType.INCREASE) or (
            self.request_type is RequestType.MODIFY and self.side is Side.BUY)

    @property
    def is_cancel(self) -> bool:
        return self.request_type is RequestType.CANCEL


def halt_rejection(h: HaltView, r: RequestView) -> str | None:
    """이 정지가 요청을 막으면 사유, 허용하면 None."""
    k = h.halt_type
    operator = r.source is RequestSource.OPERATOR
    name = f"정지 {k.value}"

    if k is HaltType.K9:
        if r.owner != h.owner:
            return None  # 다른 전략의 요청은 영향 없음
        if r.increasing:
            return f"{name}: 중지된 전략의 위험 확대"
        if r.is_cancel or operator or r.source is RequestSource.HALT_POLICY:
            return None
        if h.variant == K9Variant.REPLACE.value and r.source is RequestSource.SIGNAL:
            return None
        return f"{name}: 중지(사용자 판단) 전략은 운영 요청만"
    if k is HaltType.K10:
        if h.instrument is not None and r.instrument != h.instrument:
            return None
        return f"{name}: 데이터 품질 문제 종목의 위험 확대" if r.increasing else None  # 위험 축소는 G11이 판정

    if r.increasing:
        return f"{name}: 위험 확대 차단"
    if k is HaltType.K1:
        if operator or (r.is_cancel and r.source is RequestSource.HALT_POLICY):
            return None
        return f"{name}: 운영 요청의 감액·청산·취소만 허용"
    if k in (HaltType.K2, HaltType.K11):
        return None  # 위험 축소 전부 허용
    if k is HaltType.K3:
        if r.is_cancel and r.instrument != h.instrument:
            return None
        return f"{name}: 결과 불명 종목 외의 취소만 허용"
    if k is HaltType.K4:
        return None if r.is_cancel else f"{name}: 취소만 허용"
    if k is HaltType.K6:
        return None if operator else f"{name}: 운영 요청(사람)의 위험 축소만 허용"
    return f"{name}: 허용되는 요청 없음"  # K5·K7·K8
