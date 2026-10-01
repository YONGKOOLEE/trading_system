"""열거형 (02-data-model, 01-requirements)."""

from __future__ import annotations

from enum import StrEnum


class Mode(StrEnum):
    BACKTEST = "BACKTEST"
    PAPER = "PAPER"
    LIVE = "LIVE"


class Side(StrEnum):
    BUY = "BUY"
    SELL = "SELL"


class OrderType(StrEnum):
    MARKET = "MARKET"
    LIMIT = "LIMIT"


class RequestType(StrEnum):
    """01 D-015 요청 유형."""

    ENTRY = "ENTRY"
    INCREASE = "INCREASE"
    REDUCE = "REDUCE"
    EXIT = "EXIT"
    CANCEL = "CANCEL"
    MODIFY = "MODIFY"

    @property
    def is_new_order(self) -> bool:
        return self in (RequestType.ENTRY, RequestType.INCREASE, RequestType.REDUCE, RequestType.EXIT)

    @property
    def default_side(self) -> Side | None:
        if self in (RequestType.ENTRY, RequestType.INCREASE):
            return Side.BUY
        if self in (RequestType.REDUCE, RequestType.EXIT):
            return Side.SELL
        return None


class RequestSource(StrEnum):
    SIGNAL = "SIGNAL"
    OPERATOR = "OPERATOR"
    HALT_POLICY = "HALT_POLICY"
    EXEC_POLICY = "EXEC_POLICY"


class GateResult(StrEnum):
    APPROVED = "APPROVED"
    REJECTED = "REJECTED"


class Check(StrEnum):
    """01 §2.2 게이트 검사 번호."""

    G1 = "G1"
    G2 = "G2"
    G3 = "G3"
    G4 = "G4"
    G5 = "G5"
    G6 = "G6"
    G7 = "G7"
    G8 = "G8"
    G9 = "G9"
    G10 = "G10"
    G11 = "G11"
    G12 = "G12"
    G13 = "G13"
    G14 = "G14"
    G15 = "G15"


class IntentState(StrEnum):
    """02 ST-01. DONE은 취소·정정 의도가 브로커에서 확인된 종결 상태."""

    PENDING_SEND = "PENDING_SEND"
    SENT = "SENT"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"
    NOT_SENT = "NOT_SENT"
    NOT_ACCEPTED = "NOT_ACCEPTED"
    DONE = "DONE"

    @property
    def is_terminal(self) -> bool:
        return self in TERMINAL_INTENT_STATES


TERMINAL_INTENT_STATES = frozenset({
    IntentState.FILLED, IntentState.CANCELLED, IntentState.EXPIRED, IntentState.REJECTED,
    IntentState.NOT_SENT, IntentState.NOT_ACCEPTED, IntentState.DONE,
})
ACTIVE_INTENT_STATES = frozenset(set(IntentState) - TERMINAL_INTENT_STATES)


class SubmitOutcome(StrEnum):
    """03 D-043 전송 결과 4분류."""

    PRE_SEND_FAILURE = "PRE_SEND_FAILURE"
    REJECTED = "REJECTED"
    UNKNOWN = "UNKNOWN"
    ACCEPTED = "ACCEPTED"


class BrokerOrderState(StrEnum):
    """02 ST-03 중립 상태."""

    ACCEPTED = "ACCEPTED"
    PARTIAL = "PARTIAL"
    FILLED = "FILLED"
    CANCELLED = "CANCELLED"
    EXPIRED = "EXPIRED"
    REJECTED = "REJECTED"


class HaltType(StrEnum):
    """01 §2.6 정지 유형."""

    K1 = "K1"
    K2 = "K2"
    K3 = "K3"
    K4 = "K4"
    K5 = "K5"
    K6 = "K6"
    K7 = "K7"
    K8 = "K8"
    K9 = "K9"
    K10 = "K10"
    K11 = "K11"
