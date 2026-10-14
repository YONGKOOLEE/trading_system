"""투자자별 일별 순매수 해석·품질 (CAP-DAT-05a, CP-033 D-188·D-191, 01 R-22). 네트워크 호출 없음.

- 출처 ka10060 종목별투자자기관별차트(금액 `amt_qty_tp=1`, 순매수 `trde_tp=0`, 단위 백만원). 목록 `stk_invsr_orgn_chart`는
  최신→과거 순(T-034 실측). 연속 조회 쪽을 받은 순서대로 이어 붙여 넘긴다.
- 품질(D-191) — 무효 일자는 공급하지 않는다(전략 입력에서 "없음"):
  일자 형식 아님(그 행 버림) / 같은 일자 둘 이상 = 중복 / 순서가 어긋난 두 행의 일자 = 역순 / 투자자 필드 누락 /
  숫자 해석 불가(키움 엄격 파서) / 휴장일 행 = 이상 / 확인 범위 안의 거래일인데 행 없음 = 누락.
  투자자 합계(개인+외국인+기관계+기타법인+국가+내외국인) ≠ 0 → **경고만**(값은 유효 — 근거 없는 규칙으로 버리지 않음,
  T-034 실측에서 0 또는 ±2가 대부분이고 차이 큰 행도 있음: kiwoom-kr K-U42).
- 달력이 모르는 날(연도 없음)은 평일을 거래일로 본다(다른 공급자와 같은 보수 규칙).
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, timedelta
from decimal import Decimal

from trading_system.broker.kiwoom import parse
from trading_system.domain.investor_flow import INVESTOR_KEYS, TOTAL_KEYS, UNIT_AMOUNT, InvestorFlow

from .calendar import KrxCalendar

SOURCE = "ka10060 amt_qty_tp=1 trde_tp=0 (백만원)"
LIST_KEY = "stk_invsr_orgn_chart"


@dataclass
class FlowParse:
    flows: list[InvestorFlow] = field(default_factory=list)     # 유효 일자 (일자 오름차순)
    invalid: dict[str, str] = field(default_factory=dict)       # YYYYMMDD → 무효 사유
    warnings: list[str] = field(default_factory=list)           # 합계 ≠ 0 일자 (YYYYMMDD, 값은 유효)
    bad_rows: int = 0                                           # 일자 형식이 아닌 행 수


def is_trading_day(calendar: KrxCalendar | None, d: date) -> bool:
    known = calendar.is_trading_day(d) if calendar is not None else None
    return known if known is not None else d.weekday() < 5


def trading_days(calendar: KrxCalendar | None, first: date, last: date) -> list[date]:
    out, d = [], first
    while d <= last:
        if is_trading_day(calendar, d):
            out.append(d)
        d += timedelta(days=1)
    return out


def _day(text: str) -> date | None:
    if len(text) != 8 or not text.isascii() or not text.isdigit():
        return None
    try:
        return date(int(text[:4]), int(text[4:6]), int(text[6:]))
    except ValueError:
        return None


def _mark(invalid: dict[str, str], key: str, why: str) -> None:
    invalid[key] = why if key not in invalid else f"{invalid[key]}; {why}"


def _values(row: Mapping[str, object]) -> tuple[tuple[tuple[str, Decimal], ...] | None, str | None]:
    missing = [k for k in INVESTOR_KEYS if k not in row or row[k] is None]
    if missing:
        return None, f"필드 누락: {', '.join(missing)}"
    out: list[tuple[str, Decimal]] = []
    bad: list[str] = []
    for k in INVESTOR_KEYS:
        try:
            out.append((k, parse.dec(row[k], k)))
        except parse.KiwoomParseError:
            bad.append(k)
    if bad:
        return None, f"숫자 해석 불가: {', '.join(bad)}"
    return tuple(out), None


def parse_ka10060_rows(rows: Sequence[Mapping[str, object]], instrument: str, calendar: KrxCalendar | None, *,
                       first: date | None = None, last: date | None = None,
                       newest_first: bool = True) -> FlowParse:
    """연속 조회로 받은 행(받은 순서) → 유효 일자 값·무효 일자·경고. first~last = 누락 확인 범위(기본: 받은 일자 범위)."""
    res = FlowParse()
    keyed: list[tuple[str, date, Mapping[str, object]]] = []
    for r in rows:
        text = str(r.get("dt") or "")
        d = _day(text)
        if d is None:
            res.bad_rows += 1
            continue
        keyed.append((text, d, r))
    counts = Counter(t for t, _, _ in keyed)
    for t, n in counts.items():
        if n > 1:
            _mark(res.invalid, t, f"중복 {n}행")
    for (pt, pd, _), (t, d, _) in zip(keyed, keyed[1:]):
        if pd != d and ((d > pd) if newest_first else (d < pd)):
            _mark(res.invalid, pt, "역순(앞뒤 행 순서 어긋남)")
            _mark(res.invalid, t, "역순(앞뒤 행 순서 어긋남)")
    good: dict[date, InvestorFlow] = {}
    for t, d, r in keyed:
        if not is_trading_day(calendar, d):
            _mark(res.invalid, t, "휴장일 행(이상)")
        values, why = _values(r)
        if why is not None:
            _mark(res.invalid, t, why)
        if t in res.invalid or values is None:
            continue
        good[d] = InvestorFlow(instrument, d, UNIT_AMOUNT, values)
    seen = {d for _, d, _ in keyed}
    if seen or (first is not None and last is not None):
        lo = first if first is not None else min(seen)
        hi = last if last is not None else max(seen)
        for d in trading_days(calendar, lo, hi):
            if d not in seen:
                _mark(res.invalid, d.strftime("%Y%m%d"), "누락(거래일인데 행 없음)")
    for d in sorted(good):
        t = d.strftime("%Y%m%d")
        if t in res.invalid:     # 누락 판정과 겹칠 일은 없지만 무효가 우선
            continue
        f = good[d]
        res.flows.append(f)
        if sum((f.value(k) or Decimal(0) for k in TOTAL_KEYS), Decimal(0)) != 0:
            res.warnings.append(t)
    return res
