"""바이낸스 전송 결과 4분류 매핑 (CP-024 D-112, 03 2-M2.5, V-137).

합성 응답은 공식 문서(errors.md·rest-api.md) 형식: 오류 본문 {"code": -1013, "msg": "..."}.
"""

from __future__ import annotations

import pytest

from trading_system.broker.binance.classify import Flag, classify_order_reply, classify_query_reply, QueryKind
from trading_system.domain.enums import SubmitOutcome as O


def _err(code: int, msg: str = "x") -> dict:
    return {"code": code, "msg": msg}


# --- 명시 거부 (원문 근거 2건 + 설계 판단 1건) ---

def test_minus_1013_filter_failure_is_rejected() -> None:
    v = classify_order_reply(400, _err(-1013, "Filter failure: LOT_SIZE"), cancel=False)
    assert v.outcome is O.REJECTED and v.code == -1013 and v.flag is Flag.FILTER


def test_minus_1021_is_rejected_with_clock_flag() -> None:
    v = classify_order_reply(400, _err(-1021, "Timestamp for this request is outside of the recvWindow."), cancel=False)
    assert v.outcome is O.REJECTED and v.flag is Flag.CLOCK


def test_minus_2010_non_duplicate_message_is_rejected_design_judgment() -> None:
    v = classify_order_reply(400, _err(-2010, "Account has insufficient balance for requested action."), cancel=False)
    assert v.outcome is O.REJECTED and v.flag is Flag.NONE


@pytest.mark.parametrize("msg", ["Duplicate order sent.", "The `clOrdId` is already in use.",
                                 "The clOrdId is already in use."])
def test_minus_2010_duplicate_messages_are_unknown(msg: str) -> None:
    v = classify_order_reply(400, _err(-2010, msg), cancel=False)
    assert v.outcome is O.UNKNOWN and v.flag is Flag.DUPLICATE_ID


def test_minus_2010_without_message_is_unknown() -> None:
    v = classify_order_reply(400, {"code": -2010}, cancel=False)
    assert v.outcome is O.UNKNOWN


# --- 결과 불명 ---

@pytest.mark.parametrize("status", [500, 502, 503, 504])
def test_5xx_is_unknown_even_with_rejecting_code(status: int) -> None:
    # 5XX: "execution status is UNKNOWN" — 본문 코드가 -1013이어도 상태 우선
    assert classify_order_reply(status, _err(-1013), cancel=False).outcome is O.UNKNOWN
    assert classify_order_reply(status, None, cancel=False).outcome is O.UNKNOWN


@pytest.mark.parametrize(("status", "code", "flag"), [
    (400, -1007, Flag.NONE), (400, -1006, Flag.NONE), (400, -1000, Flag.NONE),
    (429, -1003, Flag.RATE_LIMITED), (429, None, Flag.RATE_LIMITED), (418, None, Flag.BANNED),
    (418, -1003, Flag.BANNED), (403, None, Flag.WAF),
    (400, -1015, Flag.RATE_LIMITED), (401, -1022, Flag.AUTH), (401, -2015, Flag.AUTH),
    (400, -1100, Flag.NONE), (400, -1102, Flag.NONE), (400, -1106, Flag.NONE), (400, -1111, Flag.NONE),
    (400, -9999, Flag.NONE),     # 표에 없는 코드 = 결과 불명 (기본값)
    (409, None, Flag.NONE),      # 4XX 일괄 거부 아님 (409 부분 성공)
    (404, None, Flag.NONE),
])
def test_unknown_mapping(status: int, code: int | None, flag: Flag) -> None:
    body = _err(code) if code is not None else None
    v = classify_order_reply(status, body, cancel=False)
    assert v.outcome is O.UNKNOWN
    assert v.flag is flag


def test_200_without_order_id_is_unknown() -> None:
    assert classify_order_reply(200, {"symbol": "BTCUSDT"}, cancel=False).outcome is O.UNKNOWN
    assert classify_order_reply(200, None, cancel=False).outcome is O.UNKNOWN
    assert classify_order_reply(200, {"orderId": "12"}, cancel=False).outcome is O.UNKNOWN   # 숫자 아님
    assert classify_order_reply(200, {"orderId": True}, cancel=False).outcome is O.UNKNOWN


def test_200_with_order_id_is_accepted() -> None:
    v = classify_order_reply(200, {"symbol": "BTCUSDT", "orderId": 28, "status": "FILLED"}, cancel=False)
    assert v.outcome is O.ACCEPTED and v.order_id == 28


def test_4xx_body_unparseable_is_unknown() -> None:
    assert classify_order_reply(400, None, cancel=False).outcome is O.UNKNOWN
    assert classify_order_reply(400, {"code": "x"}, cancel=False).outcome is O.UNKNOWN


# --- 취소 ---

def test_cancel_minus_2011_is_unknown_to_be_confirmed_by_query() -> None:
    v = classify_order_reply(400, _err(-2011, "Unknown order sent."), cancel=True)
    assert v.outcome is O.UNKNOWN and v.flag is Flag.CANCEL_UNCONFIRMED


def test_cancel_minus_2010_is_not_treated_as_new_order_rejection() -> None:
    assert classify_order_reply(400, _err(-2010, "x"), cancel=True).outcome is O.UNKNOWN


def test_cancel_accepted() -> None:
    v = classify_order_reply(200, {"symbol": "BTCUSDT", "orderId": 4, "status": "CANCELED"}, cancel=True)
    assert v.outcome is O.ACCEPTED


def test_reason_contains_code_not_raw_body() -> None:
    v = classify_order_reply(400, _err(-2010, "secret-ish detail " + "k" * 10), cancel=False)
    assert "-2010" in v.reason


# --- 조회 분류 (결과 불명 해소·대조 입력) ---

@pytest.mark.parametrize(("status", "body", "kind"), [
    (200, {"orderId": 1}, QueryKind.OK),
    (200, [], QueryKind.OK),
    (400, _err(-2013, "Order does not exist."), QueryKind.NOT_FOUND),
    (400, _err(-2026), QueryKind.ARCHIVED),
    (429, None, QueryKind.RATE_LIMITED),
    (418, None, QueryKind.BANNED),
    (401, _err(-2015), QueryKind.AUTH),
    (400, _err(-1022), QueryKind.AUTH),
    (400, _err(-1021), QueryKind.CLOCK),
    (500, None, QueryKind.FAILED),
    (200, None, QueryKind.FAILED),
    (400, _err(-1100), QueryKind.FAILED),
])
def test_query_kinds(status: int, body: object, kind: QueryKind) -> None:
    assert classify_query_reply(status, body).kind is kind
