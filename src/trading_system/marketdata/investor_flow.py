"""투자자별 일별 순매수 해석·품질 (CAP-DAT-05a, CP-033 D-188·D-191, 01 R-22). 네트워크 호출 없음.

- 출처 ka10060 종목별투자자기관별차트(금액 `amt_qty_tp=1`, 순매수 `trde_tp=0`, 단위 백만원). 목록 `stk_invsr_orgn_chart`는
  최신→과거 순(T-034 실측). 연속 조회 쪽을 받은 순서대로 이어 붙여 넘긴다.
- 품질(D-191) — 무효 일자는 공급하지 않는다(전략 입력에서 "없음"):
  일자 형식 아님(그 행 버림) / 같은 일자 둘 이상 = 중복 / 순서가 어긋난 두 행의 일자 = 역순 / 투자자 필드 누락 /
  숫자 해석 불가(키움 엄격 파서) / 휴장일 행 = 이상 / 확인 범위 안의 거래일인데 행 없음 = 누락.
  투자자 합계(개인+외국인+기관계+기타법인+국가+내외국인) ≠ 0 → **경고만**(값은 유효 — 근거 없는 규칙으로 버리지 않음,
  T-034 실측에서 0 또는 ±2가 대부분이고 차이 큰 행도 있음: kiwoom-kr K-U42).
- 달력이 모르는 날(연도 없음)은 평일을 거래일로 본다(다른 공급자와 같은 보수 규칙).

판정 입력 (T-036, D-189·D-193, A-100 Q-7) — PAPER(저장소)와 백테스트(InvestorFlowBook)가 같은 함수를 쓴다:
- 판정 시각 T(거래일 d)의 입력에는 **일자 < d**이고, T까지 받은 회차 중 마지막 회차가 확정 기준 시각(그 일자 다음 거래일
  06:00 KST, index_feed.confirm_at) 이후·T 이전에 관측된 값만 넣는다. T 뒤에 받은 회차·관측은 쓰지 않는다.
- 창 완전성: 저녁 수집만 하는 운영(D-192)에서 확정되어 있어야 할 마지막 일자 = 다음 거래일 저녁 수집 시각
  (max(15:45, 마감 + 15분))이 T 이전인 가장 최근 거래일. 그 일자로 끝나는 최근 N거래일이 모두 있어야 완전 —
  아니면 그 종목 판정 입력 무효(data_ok False, R-22 "무효 = 그 종목 신호 무효").
- 백테스트(과거 파일)는 관측 시각을 "그 일자 다음 거래일 저녁 수집"으로 둔다 — PAPER 저녁 수집과 같은 지연 [DP-46 임시].
"""

from __future__ import annotations

import csv
import re
from bisect import bisect_left
from collections import Counter
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field, replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

from trading_system.broker.kiwoom import parse
from trading_system.domain.clock import KST, require_aware
from trading_system.domain.investor_flow import (
    INVESTOR_KEYS, TOTAL_KEYS, UNIT_AMOUNT, FlowRevision, InvestorFlow)

from .calendar import KrxCalendar
from .index_feed import confirm_at

SOURCE = "ka10060 amt_qty_tp=1 trde_tp=0 (백만원)"
LIST_KEY = "stk_invsr_orgn_chart"
EVENING_EARLIEST = time(15, 45)           # 저녁 수집 시작 = 자동 운영 Schedule.stop_at
AFTER_CLOSE = timedelta(minutes=15)       # 늦은 마감일: 마감 + 15분 (Schedule.stop_after_close)
FLOW_DAYS_DEFAULT, FLOW_DAYS_MAX = 20, 120   # 판정 입력 창(거래일) — 전략 속성 investor_flow_days [DP-46 임시]
_CODE_FILE = re.compile(r"[0-9A-Z]{6}", re.ASCII)


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


# --- 판정 입력 (T-036, D-189·D-193) ----------------------------------------------------------------------------

def next_trading_day(calendar: KrxCalendar | None, d: date) -> date:
    n = d + timedelta(days=1)
    for _ in range(31):
        if is_trading_day(calendar, n):
            return n
        n += timedelta(days=1)
    return n


def evening_collect_at(day: date, calendar: KrxCalendar | None) -> datetime:
    """그 거래일 저녁 수집 시작 시각(UTC): max(15:45, 정규장 마감 + 15분) KST."""
    at = datetime.combine(day, EVENING_EARLIEST, tzinfo=KST)
    session = calendar.session(day) if calendar is not None else None
    if session is not None and session.is_open and session.regular_close is not None:
        at = max(at, (session.regular_close + AFTER_CLOSE).astimezone(KST))
    return at.astimezone(UTC)


def modeled_seen_at(day: date, calendar: KrxCalendar | None) -> datetime:
    """저녁 수집만 하는 운영에서 일자 day 값이 확정 근거로 처음 관측되는 시각 = 다음 거래일 저녁 수집."""
    return evening_collect_at(next_trading_day(calendar, day), calendar)


def confirmed_flows(revisions: Sequence[FlowRevision], as_of: datetime,
                    calendar: KrxCalendar | None) -> list[InvestorFlow]:
    """D-189: 일자 < 판정일, as_of까지 받은 마지막 회차, 그 회차가 [확정 기준 시각, as_of]에 관측됨. 일자 오름차순."""
    as_of = require_aware(as_of)
    d = as_of.astimezone(KST).date()
    latest: dict[date, FlowRevision] = {}
    for r in revisions:
        if r.flow.day >= d or r.collected_at > as_of:
            continue
        cur = latest.get(r.flow.day)
        if cur is None or r.revision > cur.revision:
            latest[r.flow.day] = r
    out: list[InvestorFlow] = []
    for day in sorted(latest):
        r = latest[day]
        c = confirm_at(day, calendar)
        if any(c <= s <= as_of for s in r.seen_at):
            out.append(r.flow)
    return out


def expected_last_day(as_of: datetime, calendar: KrxCalendar | None) -> date | None:
    """as_of에 확정되어 있어야 할 마지막 일자 (저녁 수집 주기 기준). 없으면 None."""
    as_of = require_aware(as_of)
    x = as_of.astimezone(KST).date() - timedelta(days=1)
    for _ in range(62):
        if is_trading_day(calendar, x) and modeled_seen_at(x, calendar) <= as_of:
            return x
        x -= timedelta(days=1)
    return None


def _needed_days(last: date, n: int, calendar: KrxCalendar | None) -> list[date]:
    out, x = [], last
    for _ in range(n * 3 + 62):
        if len(out) >= n:
            break
        if is_trading_day(calendar, x):
            out.append(x)
        x -= timedelta(days=1)
    return out


def flow_window(revisions: Sequence[FlowRevision], as_of: datetime, calendar: KrxCalendar | None,
                n: int) -> tuple[tuple[InvestorFlow, ...], bool]:
    """(확정값 최근 n개, 창 완전 여부). 완전 = expected_last_day로 끝나는 최근 n거래일이 모두 있음."""
    avail = confirmed_flows(revisions, as_of, calendar)
    flows = tuple(avail[-n:])
    last = expected_last_day(as_of, calendar)
    if last is None:
        return flows, False
    have = {f.day for f in avail}
    return flows, all(x in have for x in _needed_days(last, n, calendar))


def flow_days_of(strategy: object) -> int:
    """전략의 판정 입력 창(거래일). 속성 investor_flow_days(1~120 정수), 없으면 20. 형식이 틀리면 ValueError."""
    n = getattr(strategy, "investor_flow_days", FLOW_DAYS_DEFAULT)
    if isinstance(n, bool) or not isinstance(n, int) or not 1 <= n <= FLOW_DAYS_MAX:
        raise ValueError(f"investor_flow_days: 1~{FLOW_DAYS_MAX} 정수")
    return n


@dataclass
class InvestorFlowBook:
    """백테스트 공급용 보관(메모리). 종목 → 회차(일자·회차 오름차순). first_day~last_day = 제공 범위."""

    revisions: dict[str, list[FlowRevision]]
    first_day: date | None
    last_day: date | None
    invalid: dict[str, dict[str, str]] = field(default_factory=dict)
    _keys: dict[str, list[date]] = field(default_factory=dict, repr=False)

    def window(self, instrument: str, as_of: datetime, calendar: KrxCalendar | None,
               n: int) -> tuple[tuple[InvestorFlow, ...], bool]:
        revs = self.revisions.get(instrument, [])
        keys = self._keys.get(instrument)
        if keys is None:
            keys = self._keys[instrument] = [r.flow.day for r in revs]
        d = as_of.astimezone(KST).date()
        i = bisect_left(keys, d - timedelta(days=n * 3 + 62))
        j = bisect_left(keys, d + timedelta(days=8))     # 규칙 함수가 일자 ≥ d를 거른다(교란 검사가 그 거름을 본다)
        return flow_window(revs[i:j], as_of, calendar, n)

    def perturbed(self, from_day: date, factor: Decimal | None) -> "InvestorFlowBook":
        """룩어헤드 교란(V-259): from_day 이후(당일 포함) 값에 factor를 곱함, None이면 그 일자들을 지움."""
        out: dict[str, list[FlowRevision]] = {}
        for code, revs in self.revisions.items():
            new: list[FlowRevision] = []
            for r in revs:
                if r.flow.day < from_day:
                    new.append(r)
                elif factor is not None:
                    vals = tuple((k, v * factor) for k, v in r.flow.values)
                    new.append(replace(r, flow=replace(r.flow, values=vals)))
            out[code] = new
        return InvestorFlowBook(out, self.first_day, self.last_day, self.invalid)


def book_from_flows(flows_by_code: Mapping[str, Sequence[InvestorFlow]],
                    calendar: KrxCalendar | None) -> InvestorFlowBook:
    """과거 값(백테스트) → 보관. 관측 시각 = 그 일자 다음 거래일 저녁 수집 (modeled_seen_at)."""
    revs: dict[str, list[FlowRevision]] = {}
    days: list[date] = []
    for code, flows in flows_by_code.items():
        out = []
        for f in sorted(flows, key=lambda x: x.day):
            seen = modeled_seen_at(f.day, calendar)
            out.append(FlowRevision(f, 1, seen, (seen,)))
            days.append(f.day)
        revs[code] = out
    return InvestorFlowBook(revs, min(days) if days else None, max(days) if days else None)


def load_investor_dir(path: Path, calendar: KrxCalendar | None) -> InvestorFlowBook:
    """A-090 수집 CSV 폴더(<종목코드>.csv, 열 dt·acc_trde_prica·투자자 13구분, 일자 오름차순) → 보관.
    같은 품질 규칙(D-191) — 무효 일자는 공급하지 않는다. 종목코드 형식이 아닌 파일(meta 등)은 건너뛴다."""
    flows: dict[str, list[InvestorFlow]] = {}
    invalid: dict[str, dict[str, str]] = {}
    for f in sorted(path.glob("*.csv")):
        if not _CODE_FILE.fullmatch(f.stem):
            continue
        with f.open(encoding="utf-8", newline="") as fh:
            rows = list(csv.DictReader(fh))
        p = parse_ka10060_rows(rows, f.stem, calendar, newest_first=False)
        flows[f.stem] = p.flows
        if p.invalid:
            invalid[f.stem] = p.invalid
    book = book_from_flows(flows, calendar)
    book.invalid = invalid
    return book
