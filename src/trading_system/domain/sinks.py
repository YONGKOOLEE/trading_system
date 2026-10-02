"""정지 발동·알림의 단일 경로 (03 D-093). 실행기·해소기·대조·감시는 이 인터페이스만 쓴다."""

from __future__ import annotations

from typing import Protocol

from .enums import HaltType


class HaltSink(Protocol):
    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None, *,
                 instrument: str | None = None, owner: str | None = None, variant: str | None = None) -> str: ...


class AlertSink(Protocol):
    def alert(self, level: str, message: str, ref: str | None) -> None:
        """level: EMERGENCY / WARNING / INFO (01 §2.10)."""
        ...
