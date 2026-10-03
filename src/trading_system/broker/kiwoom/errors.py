"""키움 오류 코드 분류 (kiwoom-kr §7, 03 §4·§5.2).

근거: 키움 명세 JSON의 오류 코드 표(S2, 2026-10-03 재확인)와 공식 클라이언트 코드(S2 errors.py): 서버가 일반 코드
(예: return_code 3 "인증 실패")를 주고 구체 코드를 return_msg 안에 `[8005:...]`(REST)·`CODE=8005`(WS)로 넣는 경우가 있다.
분류는 구체 코드(본문 안 코드 우선)로 한다.
"""

from __future__ import annotations

import re
from enum import StrEnum

_EMBEDDED = re.compile(r"\[(\d{3,5}):|CODE=(\d{3,5})")
GENERIC_AUTH_CODE = 3   # 공식 클라이언트 주석 근거(인증 실패 일반 코드). 공식 표에는 없음 → 미확인 K-U12


class ErrorClass(StrEnum):
    OK = "OK"
    INPUT = "INPUT"                    # 1501·1504·1505·1511~1517, 1901~1903
    RATE_LIMIT = "RATE_LIMIT"          # 1687, 1700~1702
    AUTH = "AUTH"                      # 8001~8031(아래 제외), 8103, 일반 인증 코드
    TERMINAL = "TERMINAL"              # 8010(IP)·8040·8050(단말기) → K5 + 사용자 확인
    MODE_MISMATCH = "MODE_MISMATCH"    # 8030·8031 → K5 + 긴급 + 중단 (R-01)
    MOCK_UNSUPPORTED = "MOCK_UNSUPPORTED"  # 8104
    CORPORATE = "CORPORATE"            # 8200
    UNEXPECTED = "UNEXPECTED"          # 1999
    BUSINESS = "BUSINESS"              # 그 밖의 0 아닌 코드 (주문 업무 거부 등, 형태 미확인 K-U7)


def normalize_code(value: object) -> int | None:
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    text = str(value).strip()
    if not text or not text.lstrip("-").isdigit():
        return None
    return int(text)


def embedded_code(message: object) -> int | None:
    m = _EMBEDDED.search(str(message or ""))
    return int(m.group(1) or m.group(2)) if m else None


MOCK_UNSUPPORTED_CODE = 8104
# 실측(2026-10-03, 키움 모의 kt00005): 모의 미제공이 8104가 아니라 return_code 20 + "[2000](RC9000:모의투자에서는
# 해당업무가 제공되지 않습니다.)"로 왔다 → 8104와 같은 뜻으로 본다 (kiwoom-kr K-U8)
_MOCK_RC = "RC9000"


def specific_code(return_code: int | None, message: object) -> int | None:
    """실패 응답이면 본문 안 구체 코드를 우선한다. 성공(0)은 메시지와 무관하게 0."""
    if return_code == 0:
        return 0
    if _MOCK_RC in str(message or ""):
        return MOCK_UNSUPPORTED_CODE
    return embedded_code(message) or return_code


def classify(code: int | None) -> ErrorClass:
    if code == 0:
        return ErrorClass.OK
    if code is None:
        return ErrorClass.UNEXPECTED
    if code in (8030, 8031):
        return ErrorClass.MODE_MISMATCH
    if code in (8010, 8040, 8050):
        return ErrorClass.TERMINAL
    if 8001 <= code <= 8031 or code == 8103 or code == GENERIC_AUTH_CODE:
        return ErrorClass.AUTH
    if code == MOCK_UNSUPPORTED_CODE:
        return ErrorClass.MOCK_UNSUPPORTED
    if code == 8200:
        return ErrorClass.CORPORATE
    if code == 1999:
        return ErrorClass.UNEXPECTED
    if code == 1687 or 1700 <= code <= 1702:
        return ErrorClass.RATE_LIMIT
    if code in (1501, 1504, 1505) or 1511 <= code <= 1517 or 1901 <= code <= 1903:
        return ErrorClass.INPUT
    return ErrorClass.BUSINESS


# 이 분류가 나오면 정지 판단이 필요하다 (호출자에게 알림) — 03 §4
CRITICAL = {ErrorClass.TERMINAL, ErrorClass.MODE_MISMATCH, ErrorClass.MOCK_UNSUPPORTED, ErrorClass.CORPORATE}
