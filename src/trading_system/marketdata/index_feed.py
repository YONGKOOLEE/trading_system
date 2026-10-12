"""지수 일봉 공급 (CAP-DAT-05d, CP-034 D-199·A-092 Q-9, CP-033 D-189 시점 규칙). PAPER 공급자 계약 + 백테스트 공용.

- 출처: 키움 ka20006 업종일봉조회(inds_cd 001 KOSPI 종합). 응답 목록 `inds_dt_pole_qry`의 `dt`·`open_pric`·`high_pric`·
  `low_pric`·`cur_prc`는 소수점을 뗀 100배 정수(부호는 등락 표시라 뗀다) → 지수 = 값 / 100 (docs/broker-specs/kiwoom-kr.md
  "업종 조회" 절). `trde_qty` 단위는 미확인(K-U41) — 판정에 쓰지 않고 그대로 보관만 한다.
- 이 모듈은 받은 응답을 해석·보관·공급만 한다. **수집(네트워크 호출)은 하지 않는다** — CP-033 D-192와 같은 저녁 수집
  경로가 맡는다(이 작업 범위 밖, 실제 수집 실행 없음).
- 시점 규칙(D-189): 판정 시각 T(거래일 d)의 입력에는 일자 < d인 값만. 그 값의 수집 시각이 확정 기준 시각
  (그 일자 다음 거래일 06:00 KST) 이후여야 확정. 수집 시각이 없는 값(백테스트 과거 파일)은 확정으로 본다.
- 결측·지연 판정: 확정 값의 마지막 일자가 d의 전 거래일이 아니면 "지연", 거래일 사이 빠진 날이 있으면 "결측",
  그 밖의 품질 실패(preprocess_daily)도 판정 실패 → 배분 규칙은 판정하지 않는다(지정 전략 신규 진입 거부).
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from decimal import Decimal

from trading_system.broker.kiwoom import parse
from trading_system.domain.allocation import IndexView
from trading_system.domain.clock import KST, require_aware

from .calendar import KrxCalendar
from .quality import RawDaily, preprocess_daily

INDEX_LIST_KEY = "inds_dt_pole_qry"
INDEX_SCALE = Decimal("100")
CONFIRM_TIME = time(6, 0)        # D-189 확정 기준 시각: 그 일자 다음 거래일 06:00 KST [제안 — 실측(K-U42) 전 보수값]
MAX_MOVE = Decimal("0.30")       # 지수 일간 변동 이상치 기준 (주식과 같은 값, 품질 검사용)


class IndexParseError(ValueError):
    pass


def _value(row: dict, name: str) -> Decimal:
    try:
        return (parse.price(row.get(name), name) / INDEX_SCALE).quantize(Decimal("0.01"))
    except parse.KiwoomParseError as exc:
        raise IndexParseError(str(exc)) from None


def parse_ka20006(payload: dict) -> list[RawDaily]:
    """ka20006 응답 본문 → 지수 일봉(오래된 순). 형식이 어긋나면 추측하지 않고 IndexParseError."""
    rows = payload.get(INDEX_LIST_KEY)
    if not isinstance(rows, list):
        raise IndexParseError(f"ka20006 응답에 {INDEX_LIST_KEY} 목록이 없음")
    out: dict[date, RawDaily] = {}
    for r in rows:
        if not isinstance(r, dict):
            raise IndexParseError("ka20006 행 형식 아님")
        dt = str(r.get("dt") or "")
        if len(dt) != 8 or not dt.isdigit():
            raise IndexParseError(f"ka20006 일자 형식 아님: {dt[:12]!r}")
        day = date(int(dt[:4]), int(dt[4:6]), int(dt[6:]))
        if day in out:
            raise IndexParseError(f"ka20006 일자 중복 {day}")
        try:
            vol = parse.qty(r.get("trde_qty"), "trde_qty")
        except parse.KiwoomParseError as exc:
            raise IndexParseError(str(exc)) from None
        out[day] = RawDaily(day, _value(r, "open_pric"), _value(r, "high_pric"), _value(r, "low_pric"),
                            _value(r, "cur_prc"), vol)
    return [out[d] for d in sorted(out)]


def _is_trading_day(calendar: KrxCalendar | None, d: date) -> bool:
    known = calendar.is_trading_day(d) if calendar is not None else None
    return known if known is not None else d.weekday() < 5


def _next_trading_day(calendar: KrxCalendar | None, d: date) -> date:
    n = d + timedelta(days=1)
    for _ in range(31):
        if _is_trading_day(calendar, n):
            return n
        n += timedelta(days=1)
    return n


def _prev_trading_day(calendar: KrxCalendar | None, d: date) -> date:
    p = d - timedelta(days=1)
    for _ in range(31):
        if _is_trading_day(calendar, p):
            return p
        p -= timedelta(days=1)
    return p


def confirm_at(day: date, calendar: KrxCalendar | None) -> datetime:
    """D-189: 일자 day의 값이 확정으로 쓰이는 최소 수집 시각 (다음 거래일 06:00 KST)."""
    return datetime.combine(_next_trading_day(calendar, day), CONFIRM_TIME, tzinfo=KST)


@dataclass
class IndexBarStore:
    """지수 일봉 보관(메모리). 일자별 마지막 수집값과 수집 시각. 수집 시각 None = 과거 파일(확정)."""

    calendar: KrxCalendar | None
    _bars: dict[str, dict[date, tuple[RawDaily, datetime | None]]] = field(default_factory=dict)

    def update(self, code: str, bars: Iterable[RawDaily], collected_at: datetime | None) -> None:
        if collected_at is not None:
            require_aware(collected_at)
        book = self._bars.setdefault(code, {})
        for b in bars:
            book[b.day] = (b, collected_at)

    def view(self, code: str, as_of: datetime) -> IndexView:
        """판정 시각 as_of의 배분 규칙 입력 (D-189). 실패 사유는 issue에."""
        require_aware(as_of)
        d = as_of.astimezone(KST).date()
        book = self._bars.get(code, {})
        usable = [b for day, (b, at) in sorted(book.items())
                  if day < d and (at is None or at >= confirm_at(day, self.calendar))]
        if not usable:
            return IndexView(code, as_of, (), False, f"지수 {code} 확정 일봉 없음 (결측)")
        prepared = preprocess_daily(usable, self._trading_days, MAX_MOVE)
        if not prepared.report.ok:
            return IndexView(code, as_of, prepared.bars, False,
                             f"지수 {code} 품질 실패: {'; '.join(prepared.report.issues[:3])}")
        want = _prev_trading_day(self.calendar, d)
        if prepared.bars[-1].at != want:
            return IndexView(code, as_of, prepared.bars, False,
                             f"지수 {code} 지연: 마지막 확정 일봉 {prepared.bars[-1].at} ≠ 전 거래일 {want}")
        return IndexView(code, as_of, prepared.bars, True)

    def _trading_days(self, a: date, b: date) -> list[date]:
        out, d = [], a
        while d <= b:
            if _is_trading_day(self.calendar, d):
                out.append(d)
            d += timedelta(days=1)
        return out


def store_from_bars(code: str, bars: Sequence[RawDaily], calendar: KrxCalendar | None) -> IndexBarStore:
    """백테스트·검증 실행기: 과거 지수 파일(index/001.csv)을 확정값으로 보관."""
    s = IndexBarStore(calendar)
    s.update(code, bars, None)
    return s
