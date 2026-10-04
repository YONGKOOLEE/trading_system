"""키움 응답 값의 엄격 파서 (03 D-041). 실패하면 KiwoomParseError — 호출자가 "해석 불가"로 처리한다.

- 숫자: 좌측 0 채움·부호(+/-) 포함 문자열(예: "000000004900", "+74100", "-60000"). 공백 허용 안 함.
  빈 문자열은 숫자가 아니다(값 없음은 호출자가 따로 다룬다).
- 주문번호: 숫자만, 7자리 이하 → 7자리로 0 채움 (주문 응답 예시는 5자리, 조회는 7자리라 맞춘다).
- 종목코드: 조회 응답의 접두어(A 주식, Q ETN, J ELW)를 떼고 6자리 숫자만 받는다. 거래소 접미사(_NX, _AL)는 거부.
- 시각: KST 기준. 응답에는 시각만 있으므로 날짜는 호출자가 준다.
"""

from __future__ import annotations

import re
from datetime import date, datetime, time
from decimal import Decimal, InvalidOperation

from trading_system.domain.clock import KST

_NUM = re.compile(r"[+-]?\d+(\.\d+)?", re.ASCII)  # 전각 숫자 등 거부
_CODE = re.compile(r"[AQJ]?(\d{6})", re.ASCII)


class KiwoomParseError(ValueError):
    pass


def dec(value: object, field: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        raise KiwoomParseError(f"{field}: 숫자 문자열이 아님 ({type(value).__name__})")
    text = str(value)
    if not _NUM.fullmatch(text):
        raise KiwoomParseError(f"{field}: 숫자 형식 아님")
    try:
        return Decimal(text)
    except InvalidOperation as exc:  # pragma: no cover - 정규식이 먼저 막음
        raise KiwoomParseError(f"{field}: 숫자 변환 실패") from exc


def qty(value: object, field: str) -> Decimal:
    """수량: 정수, 0 이상 (부호 '+'는 허용, 음수는 거부)."""
    q = dec(value, field)
    if q < 0 or q != q.to_integral_value():
        raise KiwoomParseError(f"{field}: 수량은 0 이상 정수")
    return q.quantize(Decimal("1"))


def price(value: object, field: str) -> Decimal:
    """가격: 부호는 등락 표시라 떼고(시세 필드), 절댓값을 쓴다. 정수 원 단위."""
    p = abs(dec(value, field))
    if p != p.to_integral_value():
        raise KiwoomParseError(f"{field}: 가격은 정수 원")
    return p.quantize(Decimal("1"))


def order_no(value: object, field: str = "ord_no") -> str:
    if not isinstance(value, str) or not (value.isascii() and value.isdigit()) or not 1 <= len(value) <= 7:
        raise KiwoomParseError(f"{field}: 주문번호 형식 아님")
    return value.zfill(7)


def fill_no(value: object, field: str) -> str | None:
    """체결번호(kt00009 cntr_no·실시간 909) 정규화 — 두 경로가 같은 함수를 쓴다 (RV-14, K-U14).

    공백 제거 후 숫자만 받는다. 앞자리 0 개수가 경로마다 달라도 같은 값이 되도록 정수로 읽어 7자리 이상 0 채움
    (주문번호 규칙과 같은 폭). 빈 값·전부 0 → None(체결번호 없음). 숫자가 아니면 해석 불가.
    """
    if value is None:
        return None
    if not isinstance(value, str):
        raise KiwoomParseError(f"{field}: 체결번호 형식 아님")
    text = value.strip()
    if text == "":
        return None
    if not (text.isascii() and text.isdigit()):
        raise KiwoomParseError(f"{field}: 체결번호 형식 아님")
    n = int(text)
    return None if n == 0 else str(n).zfill(7)


def order_price(value: object, field: str, market: bool) -> Decimal:
    """주문 가격. 시장가 주문은 가격이 없을 수 있다(빈 문자열) → 0 (RV-13, K-U17). 지정가의 빈 값은 해석 불가."""
    if market and (value is None or (isinstance(value, str) and value.strip() == "")):
        return Decimal("0")
    return price(value, field)


def optional_order_no(value: object, field: str) -> str | None:
    """원주문번호: 없으면 '0000000' 또는 빈 값 → None."""
    if value in (None, ""):
        return None
    no = order_no(value, field)
    return None if no == "0000000" else no


def stock_code(value: object, field: str = "stk_cd") -> str:
    if not isinstance(value, str):
        raise KiwoomParseError(f"{field}: 종목코드 형식 아님 ({type(value).__name__})")
    m = _CODE.fullmatch(value.strip())
    if m is None:
        # 종목코드는 비민감 → 진단용으로 값(20자까지)을 남긴다 (2026-10-04 예행연습 ka10054)
        shown = value if len(value) <= 20 else value[:20] + "…"
        raise KiwoomParseError(f"{field}: 종목코드 형식 아님 ({shown!r})")
    return m.group(1)


def hms(value: object, day: date, field: str) -> datetime:
    """'HHmmss' 또는 'HH:mm:ss' + 거래일 → KST 시각."""
    if not isinstance(value, str):
        raise KiwoomParseError(f"{field}: 시각 형식 아님")
    text = value.replace(":", "")
    if len(text) != 6 or not (text.isascii() and text.isdigit()):
        raise KiwoomParseError(f"{field}: 시각 형식 아님")
    try:
        t = time(int(text[:2]), int(text[2:4]), int(text[4:]))
    except ValueError as exc:
        raise KiwoomParseError(f"{field}: 시각 값 범위 밖") from exc
    return datetime.combine(day, t, tzinfo=KST)


def expires(value: object) -> datetime:
    """토큰 만료 시각 expires_dt (YYYYMMDDHHmmss, KST — kiwoom-kr §1)."""
    if not isinstance(value, str) or len(value) != 14 or not (value.isascii() and value.isdigit()):
        raise KiwoomParseError("expires_dt: 형식 아님")
    try:
        return datetime.strptime(value, "%Y%m%d%H%M%S").replace(tzinfo=KST)
    except ValueError as exc:
        raise KiwoomParseError("expires_dt: 값 범위 밖") from exc


def side_from_text(text: object, field: str) -> str:
    """'현금매수'·'+매수'·'매도정정' 등 → BUY/SELL. 둘 다 있거나 없으면 해석 불가."""
    s = str(text or "")
    buy, sell = "매수" in s, "매도" in s
    if buy == sell:
        raise KiwoomParseError(f"{field}: 매수·매도 구분 불가")
    return "BUY" if buy else "SELL"


def kind_from_text(*texts: object) -> str:
    """정정/취소 구분 → NEW/CANCEL/MODIFY. 정정·취소가 같이 나오면 해석 불가."""
    s = " ".join(str(t or "") for t in texts)
    cancel, modify = "취소" in s, "정정" in s
    if cancel and modify:
        raise KiwoomParseError("정정·취소 구분 불가")
    return "CANCEL" if cancel else ("MODIFY" if modify else "NEW")
