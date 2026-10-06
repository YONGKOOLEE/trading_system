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


class HaltControl(HaltSink, Protocol):
    """정지 발동 + 재개 조건 보고·재개. 전략 상태 전환(K9)처럼 정지를 풀어야 하는 쪽이 쓴다 (01 K9 v0.5)."""

    def report_condition(self, halt_type: HaltType, account: str | None, name: str, ok: bool, evidence: str = "", *,
                         instrument: str | None = None, owner: str | None = None,
                         recon: str | None = None) -> list[str]: ...

    def resume(self, halt_type: HaltType, account: str | None, channel: str, actor: str, *,
               owner: str | None = None) -> object: ...
