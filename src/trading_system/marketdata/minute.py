"""분봉 공급·품질 (CP-032 D-177, T-028, V-241~V-243, 승인 A-095). 판정 입력 배선(주기 판정)은 T-029 — 아직 엔진에 연결하지 않는다.

출처: 키움 ka10080 주식분봉차트조회요청 (docs/broker-specs/kiwoom-kr.md E1, K-U30~K-U33·K-U43 2026-10-12 모의 실측)
- 목록 키 `stk_min_pole_chart_qry`, 응답은 최신→과거, 1쪽 900건, `cntr_tm` YYYYMMDDHHMMSS(초 00), 제공 약 1년.
- 가격(`open_pric`·`high_pric`·`low_pric`·`cur_prc`)은 **부호를 떼고**(등락 표시) Decimal 정수 원(parse.price).
  거래량 `trde_qty`는 부호를 그대로 읽어(정수) 음수는 품질 검사가 잡는다. 필수 필드 누락·형식 오류 → 조회 실패(누락).

순서 (prepare_minutes)
1. 파싱(받은 순서 유지) → 2. 정규장 봉만 → 3. 확정 봉만 → 4. 날짜별 품질 → 5. 무효인 날 제외, 시각 오름차순.
품질을 확정 봉에만 적용하므로 진행 중인 최신 봉이 어떤 값이어도 결과가 같다(V-242, 일봉 V-91과 같은 원칙).

정규장 봉만 (A-095, K-U43) [제안 규칙]
- 봉 [start, start + 길이)가 KRX 달력의 그날 정규장 [개장, 마감) 안에 **완전히** 들어가는 봉만 쓴다(달력의 개장 시간
  변경일 — 수능일·연초 — 포함). 마감 동시호가 체결 봉(15:30)·장 마감 뒤 봉(15:35, NXT·시간외로 추정되는 16:00~19:59)·
  장 시작 전 봉은 "정규장 밖", 휴장일 봉은 "휴장일", 달력이 없는 날(연도 파일 없음·실시간 교차 확인 충돌)은 "달력 없음"으로
  세고 버린다(불확실하면 쓰지 않음). 30·45·60분봉의 마감을 넘는 마지막 봉(예: 60분 15:00)도 버린다.

봉 확정 (D-177 보수 규칙): 판정 시각 T에는 봉 끝(= start + 길이) ≤ T − 확정 여유(기본 5초)인 봉만.

품질 (R-22, D-177) — 그 종목 그 날 무효
- 같은 시각 중복, 역순(응답은 최신→과거여야 함), 음수 거래량, 0 이하 가격, 고가·저가 모순.
- 정규장 안에서 봉이 없는 분은 누락이 아니라 "거래 없음"(유효) — 날짜별 빈 칸 수만 센다.
- 연속 조회 쪽 실패·빈 목록·필드 누락 = 누락(무효, missing).
- 무효·누락은 일봉과 같은 K10 경로(종목 범위 정지 발동, store.update_daily와 같은 호출)로 보낼 수 있다(report_k10).
  해제 보고는 하지 않는다 — 분봉 정상이 일봉 품질 K10을 풀면 안 되므로 해제 조건은 T-029에서 정한다 [제안].
"""

from __future__ import annotations

import csv
import os
import re
from collections import Counter
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from trading_system.broker.kiwoom import parse
from trading_system.domain.clock import KST
from trading_system.domain.enums import HaltType
from trading_system.domain.minute_bar import MinuteBar
from trading_system.domain.sinks import HaltSink

from .calendar import KrxCalendar
from .errors import MarketDataError

MIN_LIST_KEY = "stk_min_pole_chart_qry"
MINUTE_SCOPES = (1, 3, 5, 10, 15, 30, 45, 60)          # ka10080 tic_scope (S2 명세)
REQUIRED_FIELDS = ("cntr_tm", "open_pric", "high_pric", "low_pric", "cur_prc", "trde_qty")
CONFIRM_MARGIN = timedelta(seconds=5)                   # D-177 확정 여유 기본 제안값
MINUTE_RULES_VERSION = "mb-1"                           # 규칙(정규장·확정·품질)을 바꾸면 올린다
MINUTE_CSV_HEADER = ("datetime", "open", "high", "low", "close", "volume")
OUTSIDE, HOLIDAY, UNKNOWN, UNCONFIRMED = "정규장 밖", "휴장일", "달력 없음", "미확정"

_TS = re.compile(r"[0-9]{14}", re.ASCII)


class MinuteDataMissing(MarketDataError):
    """분봉 조회 실패(누락) — 필드 누락·형식 오류·빈 목록·쪽 실패. 그 종목 분봉은 쓰지 않는다."""


# --- 파싱 (V-241) ----------------------------------------------------------------------------------------

def _start(value: object, where: str) -> datetime:
    text = value if isinstance(value, str) else ""
    if not _TS.fullmatch(text):
        raise MinuteDataMissing(f"{where}: cntr_tm 형식 아님(YYYYMMDDHHMMSS)")
    try:
        return datetime(int(text[:4]), int(text[4:6]), int(text[6:8]), int(text[8:10]), int(text[10:12]),
                        int(text[12:]), tzinfo=KST)
    except ValueError:
        raise MinuteDataMissing(f"{where}: cntr_tm 날짜·시각 아님") from None


def _volume(value: object, name: str) -> Decimal:
    v = parse.dec(value, name)
    if v != v.to_integral_value():
        raise parse.KiwoomParseError(f"{name}: 거래량은 정수")
    return v.quantize(Decimal("1"))


def parse_minute_rows(rows: Sequence[object], instrument: str, minutes: int, adjusted: str = "1") -> list[MinuteBar]:
    """응답 행 → 분봉(받은 순서 그대로). 필드 누락·형식 오류면 MinuteDataMissing (값은 메시지에 넣지 않는다)."""
    out: list[MinuteBar] = []
    for i, r in enumerate(rows, start=1):
        where = f"{instrument} {i}번째 행"
        if not isinstance(r, Mapping):
            raise MinuteDataMissing(f"{where}: 행 형식 아님")
        absent = [f for f in REQUIRED_FIELDS if f not in r or r[f] is None]
        if absent:
            raise MinuteDataMissing(f"{where}: {', '.join(absent)} 없음")
        try:
            out.append(MinuteBar(instrument, minutes, _start(r["cntr_tm"], where),
                                 parse.price(r["open_pric"], "open_pric"), parse.price(r["high_pric"], "high_pric"),
                                 parse.price(r["low_pric"], "low_pric"), parse.price(r["cur_prc"], "cur_prc"),
                                 _volume(r["trde_qty"], "trde_qty"), adjusted))
        except parse.KiwoomParseError as exc:
            raise MinuteDataMissing(f"{where}: {exc}") from None
    return out


def ascending(bars: Iterable[MinuteBar]) -> list[MinuteBar]:
    return sorted(bars, key=lambda b: b.start)


def parse_minute_pages(pages: Sequence[Sequence[object]], instrument: str, minutes: int,
                       adjusted: str = "1") -> list[MinuteBar]:
    """연속 조회 쪽들을 이어 붙여 파싱하고 시각 오름차순으로 돌려준다."""
    return ascending(parse_minute_rows([r for p in pages for r in p], instrument, minutes, adjusted))


# --- 정규장·확정 -------------------------------------------------------------------------------------------

def kst_day(bar: MinuteBar) -> date:
    return bar.start.astimezone(KST).date()


def split_regular(bars: Sequence[MinuteBar], calendar: KrxCalendar) -> tuple[list[MinuteBar], dict[str, int]]:
    """(정규장 안 봉 — 받은 순서 유지, 제외 사유별 건수). 봉 [start, end)가 [개장, 마감) 안에 완전히 들어가야 한다."""
    kept: list[MinuteBar] = []
    excluded: Counter[str] = Counter()
    for b in bars:
        s = calendar.session(kst_day(b))
        if s is None:
            excluded[UNKNOWN] += 1
        elif not s.is_open or s.regular_open is None or s.regular_close is None:
            excluded[HOLIDAY] += 1
        elif s.regular_open <= b.start and b.end <= s.regular_close:
            kept.append(b)
        else:
            excluded[OUTSIDE] += 1
    return kept, dict(excluded)


def confirmed_bars(bars: Sequence[MinuteBar], as_of: datetime,
                   margin: timedelta = CONFIRM_MARGIN) -> list[MinuteBar]:
    """판정 시각 as_of에 확정된 봉만: 봉 끝 ≤ as_of − margin (D-177, V-242)."""
    limit = as_of - margin
    return [b for b in bars if b.end <= limit]


# --- 품질 (V-243) ------------------------------------------------------------------------------------------

def _hm(b: MinuteBar) -> str:
    return b.start.astimezone(KST).strftime("%H:%M")


def day_issues(received: Sequence[MinuteBar]) -> dict[date, list[str]]:
    """받은 순서의 봉 → 날짜별 문제. 응답은 최신→과거여야 하므로 앞 봉보다 늦은 봉은 역순이다."""
    issues: dict[date, list[str]] = {}

    def add(b: MinuteBar, text: str) -> None:
        issues.setdefault(kst_day(b), []).append(f"{_hm(b)} {text}")

    counts = Counter(b.start for b in received)
    for b in received:
        if counts[b.start] > 1:
            add(b, "같은 시각 중복")
            counts[b.start] = 0        # 같은 시각은 한 번만 적는다
    for prev, cur in zip(received, received[1:], strict=False):
        if cur.start > prev.start:
            add(cur, "역순(최신→과거 순서 아님)")
    for b in received:
        if b.volume < 0:
            add(b, "음수 거래량")
        if min(b.open, b.high, b.low, b.close) <= 0:
            add(b, "0 이하 가격")
        if not (b.low <= min(b.open, b.close) and max(b.open, b.close) <= b.high):
            add(b, "고가·저가 모순")
    return issues


def no_trade_slots(bars: Sequence[MinuteBar], calendar: KrxCalendar, minutes: int) -> dict[date, int]:
    """날짜별 정규장 안 빈 칸 수(거래 없음 — 누락 아님). 칸 수 = 정규장 길이 ÷ 봉 길이(마감을 넘는 칸 제외)."""
    present = Counter(kst_day(b) for b in bars)
    out: dict[date, int] = {}
    for d, n in sorted(present.items()):
        s = calendar.session(d)
        if s is None or s.regular_open is None or s.regular_close is None:
            continue
        slots = int((s.regular_close - s.regular_open) / timedelta(minutes=minutes))
        out[d] = max(slots - n, 0)
    return out


@dataclass(frozen=True)
class MinuteSupply:
    """한 종목 분봉 공급 결과. data_ok가 거짓이면 그 종목 분봉 판정은 무효(K10 경로)."""

    instrument: str
    minutes: int
    adjusted: str
    as_of: datetime
    bars: tuple[MinuteBar, ...]                                  # 정규장·확정·유효한 날만, 시각 오름차순
    data_ok: bool
    missing: bool = False                                        # 조회 실패(누락)
    reason: str = ""
    invalid_days: tuple[tuple[date, tuple[str, ...]], ...] = ()  # 품질 무효인 날과 문제
    excluded: dict[str, int] = field(default_factory=dict)       # 정규장 밖·휴장일·달력 없음·미확정 건수
    no_trade_slots: dict[date, int] = field(default_factory=dict)
    version: str = MINUTE_RULES_VERSION


def missing_supply(instrument: str, minutes: int, as_of: datetime, reason: str, adjusted: str = "1") -> MinuteSupply:
    """연속 조회 쪽 실패 등 — 누락(무효)."""
    return MinuteSupply(instrument, minutes, adjusted, as_of, (), False, True, reason)


def prepare_minutes(rows: Sequence[object], instrument: str, minutes: int, calendar: KrxCalendar, as_of: datetime,
                    adjusted: str = "1", margin: timedelta = CONFIRM_MARGIN) -> MinuteSupply:
    """받은 응답 행(연속 조회를 이어 붙인 것, 최신→과거) → 공급 결과."""
    if not rows:
        return missing_supply(instrument, minutes, as_of, "빈 응답 목록 — 누락", adjusted)
    try:
        received = parse_minute_rows(rows, instrument, minutes, adjusted)
    except MinuteDataMissing as exc:
        return missing_supply(instrument, minutes, as_of, str(exc), adjusted)
    regular, excluded = split_regular(received, calendar)
    final = confirmed_bars(regular, as_of, margin)
    if len(final) < len(regular):
        excluded[UNCONFIRMED] = len(regular) - len(final)
    issues = day_issues(final)
    good = ascending(b for b in final if kst_day(b) not in issues)
    return MinuteSupply(instrument, minutes, adjusted, as_of, tuple(good), not issues,
                        invalid_days=tuple((d, tuple(v)) for d, v in sorted(issues.items())), excluded=excluded,
                        no_trade_slots=no_trade_slots(good, calendar, minutes))


def report_k10(halts: HaltSink, account: str | None, supply: MinuteSupply) -> bool:
    """무효·누락이면 그 종목 K10 발동(일봉 store.update_daily와 같은 호출·같은 범위). 발동했으면 True."""
    if supply.data_ok:
        return False
    if supply.missing:
        reason = f"분봉 데이터 누락 {supply.instrument}: {supply.reason}"
    else:
        d, issues = supply.invalid_days[0]
        reason = f"분봉 데이터 품질 실패 {supply.instrument}: {d} {'; '.join(issues[:3])}"
    halts.activate(HaltType.K10, "marketdata", reason, account, instrument=supply.instrument)
    return True


# --- CSV (백테스트·연구용) -----------------------------------------------------------------------------------

def minute_csv_name(code: str, minutes: int) -> str:
    return f"{code}-{minutes}m.csv"


def _csv_time(b: MinuteBar) -> str:
    return b.start.astimezone(KST).isoformat()


def read_minute_csv(path: Path, instrument: str, minutes: int, adjusted: str = "1") -> list[MinuteBar]:
    """이 모듈이 쓴 CSV만 읽는다. 머리 줄이 다르거나 형식이 아니면 MinuteDataMissing."""
    with path.open(encoding="utf-8", newline="") as f:
        reader = csv.reader(f)
        header = next(reader, None)
        if header is None or tuple(header) != MINUTE_CSV_HEADER:
            raise MinuteDataMissing(f"{path.name}: 머리 줄이 {','.join(MINUTE_CSV_HEADER)}가 아님 — 다른 파일")
        out: list[MinuteBar] = []
        for i, r in enumerate(reader, start=2):
            try:
                start = datetime.fromisoformat(r[0])
                o, h, lo, c, v = (Decimal(x) for x in r[1:6])
                out.append(MinuteBar(instrument, minutes, start, o, h, lo, c, v, adjusted))
            except (ValueError, IndexError, ArithmeticError):
                raise MinuteDataMissing(f"{path.name}:{i}: 형식 아님") from None
    return out


@dataclass(frozen=True)
class MergeResult:
    added: int
    skipped: int        # 이미 있던 시각 (아래 differing 포함)
    differing: int      # 이미 있던 시각인데 값이 다름 — 기존 값 유지 (수정주가 재계산 가능성, 사람 확인)


def _values(b: MinuteBar) -> tuple[Decimal, ...]:
    return (b.open, b.high, b.low, b.close, b.volume)


def merge_minute_csv(path: Path, bars: Sequence[MinuteBar]) -> MergeResult:
    """기존 CSV에 새 봉만 더한다(이미 있는 시각은 건너뜀, 기존 값 유지). 시각 오름차순으로 임시 파일에 쓴 뒤 교체."""
    existing: dict[datetime, MinuteBar] = {}
    if path.exists():
        if not bars:
            return MergeResult(0, 0, 0)
        first = bars[0]
        existing = {b.start: b for b in read_minute_csv(path, first.instrument, first.minutes, first.adjusted)}
    added: dict[datetime, MinuteBar] = {}
    skipped = differing = 0
    for b in bars:
        old = existing.get(b.start)
        if old is not None:
            skipped += 1
            differing += _values(old) != _values(b)
        elif b.start not in added:
            added[b.start] = b
    if added:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        with tmp.open("w", encoding="utf-8", newline="") as f:
            w = csv.writer(f, lineterminator="\n")
            w.writerow(MINUTE_CSV_HEADER)
            for b in ascending([*existing.values(), *added.values()]):
                w.writerow([_csv_time(b), b.open, b.high, b.low, b.close, b.volume])
        os.replace(tmp, path)
    return MergeResult(len(added), skipped, differing)
