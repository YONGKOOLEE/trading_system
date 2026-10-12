"""바이낸스 응답 분류 — 주문 변경 4분류(CP-024 D-112, 03 2-M2.5)와 조회 분류(D-111·D-113).

주문 변경 요청(신규·취소)의 판정 순서
1. HTTP 5XX → 결과 불명 (원문 "execution status is UNKNOWN", 본문 코드와 무관).
2. HTTP 200 + 정수 orderId → 접수. 200인데 orderId 없음·본문 해석 불가 → 결과 불명.
3. 오류 코드 매핑(표에 있는 것만 명시 거부):
   - -1013 → 명시 거부 (원문 "didn't reach the Matching Engine")
   - -1021 → 명시 거부 (Timing security 의사코드: 매칭엔진 전달 전 reject) + 시계 표시
   - 신규 주문의 -2010: "Duplicate order sent."·"The clOrdId is already in use." → 결과 불명(같은 ID 주문이 있을 수 있음),
     그 밖의 메시지 → 명시 거부 (**설계 판단** — 원문은 "matching engine이 오류 반환" 머리말뿐). 메시지 없음 → 결과 불명.
   - 취소의 -2011 → 결과 불명(대상이 이미 체결·만료됐을 수 있음, 주문 조회로 확정)
4. 그 밖의 모든 것(403·409·429·418, -1000·-1003·-1006·-1007·-1015·-1022·-2015·-1100~-1106·-1111, 표에 없는 코드,
   본문 해석 불가) → 결과 불명 (B-U20: 실행 여부 원문 문구 없음). HTTP 4XX를 일괄 명시 거부로 보지 않는다.
표시(Flag)는 분류와 별개로 호출자의 후속 처리(429 쿨다운, 418 전체 중단·K5, -1021 K6 판정, 인증 K3·K5)에 쓴다.

메시지 비교는 문서 표기의 백틱을 떼고 앞뒤 공백을 무시한다. reason에는 HTTP 상태·코드만 넣는다(응답 원문 금지).
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum

from trading_system.domain.enums import SubmitOutcome

_DUPLICATE_MESSAGES = frozenset({"Duplicate order sent.", "The clOrdId is already in use."})
_AUTH_CODES = frozenset({-1022, -2015})
_RATE_CODES = frozenset({-1003, -1015})


class Flag(StrEnum):
    NONE = "NONE"
    FILTER = "FILTER"                      # -1013: 규칙 재적재 + 알림 (D-106)
    CLOCK = "CLOCK"                        # -1021: 시계 경고, 연속 3회 K6 판정 요청 (D-115)
    DUPLICATE_ID = "DUPLICATE_ID"          # -2010 중복 메시지: 그 ID로 조회 (D-111)
    CANCEL_UNCONFIRMED = "CANCEL_UNCONFIRMED"  # 취소 -2011: 주문 조회로 상태 확정
    RATE_LIMITED = "RATE_LIMITED"          # 429·-1003·-1015
    BANNED = "BANNED"                      # 418: 전체 호출 중단·K5·사람 재개 (D-113)
    WAF = "WAF"                            # 403
    AUTH = "AUTH"                          # -1022·-2015


@dataclass(frozen=True)
class OrderVerdict:
    outcome: SubmitOutcome
    flag: Flag
    reason: str
    code: int | None = None
    order_id: int | None = None


def error_code(body: object) -> tuple[int | None, str | None]:
    """오류 본문 {"code": int, "msg": str}에서 (코드, 메시지). 형식이 아니면 (None, None)."""
    if not isinstance(body, dict):
        return None, None
    code = body.get("code")
    if isinstance(code, bool) or not isinstance(code, int):
        return None, None
    msg = body.get("msg")
    return code, msg if isinstance(msg, str) else None


def _status_flag(status: int, code: int | None) -> Flag:
    if status == 418:
        return Flag.BANNED
    if status == 429 or code in _RATE_CODES:
        return Flag.RATE_LIMITED
    if status == 403:
        return Flag.WAF
    if code in _AUTH_CODES:
        return Flag.AUTH
    return Flag.NONE


def _is_duplicate_message(msg: str) -> bool:
    return msg.replace("`", "").strip() in _DUPLICATE_MESSAGES


def classify_order_reply(status: int, body: object, *, cancel: bool) -> OrderVerdict:
    code, msg = error_code(body)
    where = f"HTTP {status}" + (f", 코드 {code}" if code is not None else "")
    U, R = SubmitOutcome.UNKNOWN, SubmitOutcome.REJECTED
    if status >= 500:
        return OrderVerdict(U, Flag.NONE, f"{where}: 서버 내부 오류 — 실행 상태 불명", code)
    if status == 200:
        oid = body.get("orderId") if isinstance(body, dict) else None
        if isinstance(oid, int) and not isinstance(oid, bool):
            return OrderVerdict(SubmitOutcome.ACCEPTED, Flag.NONE, f"{where}: 접수", None, oid)
        return OrderVerdict(U, Flag.NONE, f"{where}: orderId 없음·해석 불가 — 결과 불명", code)
    if code == -1013:
        return OrderVerdict(R, Flag.FILTER, f"{where}: 매칭엔진 미도달 거부(필터 등)", code)
    if code == -1021:
        return OrderVerdict(R, Flag.CLOCK, f"{where}: 시각 검증 거부(recvWindow)", code)
    if code == -2010 and not cancel:
        if msg is None:
            return OrderVerdict(U, Flag.NONE, f"{where}: 메시지 없음 — 중복 여부 판정 불가", code)
        if _is_duplicate_message(msg):
            return OrderVerdict(U, Flag.DUPLICATE_ID, f"{where}: 같은 고객 주문 ID 주문 존재 가능 — 조회 필요", code)
        return OrderVerdict(R, Flag.NONE, f"{where}: 신규 주문 거부(설계 판단 D-112)", code)
    if code == -2011 and cancel:
        return OrderVerdict(U, Flag.CANCEL_UNCONFIRMED, f"{where}: 취소 거부 — 대상 상태 조회 필요", code)
    return OrderVerdict(U, _status_flag(status, code), f"{where}: 실행 여부 근거 없음 — 결과 불명", code)


class QueryKind(StrEnum):
    OK = "OK"
    NOT_FOUND = "NOT_FOUND"        # -2013 (접수 직후 지연 가능성 B-U3 — "미접수" 확정 아님)
    ARCHIVED = "ARCHIVED"          # -2026
    RATE_LIMITED = "RATE_LIMITED"  # 429: Retry-After 대기 후 최대 3회 → K5
    BANNED = "BANNED"              # 418: 즉시 중단·K5
    AUTH = "AUTH"                  # -1022·-2015: 재시도 없음 → K3·K5 (호출자)
    CLOCK = "CLOCK"                # -1021
    FAILED = "FAILED"              # 5XX·해석 불가·그 밖의 오류


@dataclass(frozen=True)
class QueryVerdict:
    kind: QueryKind
    reason: str
    code: int | None = None


def classify_query_reply(status: int, body: object) -> QueryVerdict:
    code, _ = error_code(body)
    where = f"HTTP {status}" + (f", 코드 {code}" if code is not None else "")
    if status == 200:
        ok = isinstance(body, (dict, list))
        return QueryVerdict(QueryKind.OK if ok else QueryKind.FAILED, where, None)
    if status == 418:
        return QueryVerdict(QueryKind.BANNED, where, code)
    if status == 429 or code in _RATE_CODES:
        return QueryVerdict(QueryKind.RATE_LIMITED, where, code)
    if status >= 500:
        return QueryVerdict(QueryKind.FAILED, where, code)
    kind = {-2013: QueryKind.NOT_FOUND, -2026: QueryKind.ARCHIVED, -1021: QueryKind.CLOCK,
            -1022: QueryKind.AUTH, -2015: QueryKind.AUTH}.get(code if code is not None else 0, QueryKind.FAILED)
    return QueryVerdict(kind, where, code)
