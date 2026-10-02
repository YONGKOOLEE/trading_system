"""시장 달력 (R-14, 03 D-055·§11, 02 ENT-04). M1: KRX.

- 원본은 연도별 설정 파일 `config/calendar/krx-<연도>.toml` (KRX 휴장일·개장 시간 변경 공지, X-U3).
  파일이 없는 연도는 "모름"(None) → 게이트 G12가 모든 주문을 거부한다 (불확실하면 주문하지 않음).
- 기본 정규장 09:00~15:30 KST, 월~금. 휴장일과 시간 변경일은 파일이 덮어쓴다.
- 실시간 장운영 통보(키움 0s)와 교차 확인: 그날 달력과 다르면 그날은 "모름"으로 바꾸고 알림 (V-75).
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, time
from pathlib import Path

from trading_system.domain.clock import KST
from trading_system.domain.limits import KRX_REGULAR_CLOSE, KRX_REGULAR_OPEN
from trading_system.domain.market import SessionInfo

from .errors import MarketDataError


@dataclass(frozen=True)
class YearCalendar:
    year: int
    source: str
    source_version: str
    holidays: frozenset[date]
    special: dict[date, tuple[time, time]] = field(default_factory=dict)  # 개장 시간 변경일


def _hm(text: object, where: str) -> time:
    if not isinstance(text, str) or len(text) != 5 or text[2] != ":":
        raise MarketDataError(f"{where}: HH:MM 형식이어야 함")
    return time(int(text[:2]), int(text[3:]))


def parse_calendar(raw: dict, expected_year: int | None = None) -> YearCalendar:
    problems: list[str] = []
    for key in raw:
        if key not in ("market", "year", "source", "source_version", "holidays", "special_sessions"):
            problems.append(f"{key}: 알 수 없는 키")
    if raw.get("market") != "KRX":
        problems.append("market: KRX여야 함")
    year = raw.get("year")
    if not isinstance(year, int) or isinstance(year, bool):
        problems.append("year: 정수")
    elif expected_year is not None and year != expected_year:
        problems.append("year: 파일 이름의 연도와 다름")
    for k in ("source", "source_version"):
        if not isinstance(raw.get(k), str) or not raw.get(k):
            problems.append(f"{k}: 출처를 적어야 함")
    holidays: set[date] = set()
    for d in raw.get("holidays", []):
        if not isinstance(d, date) or isinstance(d, datetime):
            problems.append(f"holidays: 날짜 형식이 아님 ({d!r})")
        elif isinstance(year, int) and d.year != year:
            problems.append(f"holidays: 다른 연도 날짜 {d}")
        else:
            holidays.add(d)
    special: dict[date, tuple[time, time]] = {}
    for item in raw.get("special_sessions", []):
        try:
            d = item["date"]
            o, c = _hm(item["open"], f"special {d}"), _hm(item["close"], f"special {d}")
        except (KeyError, TypeError, MarketDataError) as exc:
            problems.append(f"special_sessions: {exc}")
            continue
        if not o < c:
            problems.append(f"special_sessions {d}: 시작 < 종료")
        special[d] = (o, c)
    if problems:
        raise MarketDataError(problems)
    return YearCalendar(year, raw["source"], raw["source_version"], frozenset(holidays), special)  # type: ignore[arg-type]


class KrxCalendar:
    def __init__(self, years: dict[int, YearCalendar] | None = None) -> None:
        self._years = dict(years or {})
        self._live_closed: dict[date, bool] = {}  # 0s 교차 확인 결과: 날짜 → 실시간이 말하는 휴장 여부
        self.conflicts: list[date] = []

    @classmethod
    def load_dir(cls, directory: Path) -> KrxCalendar:
        years: dict[int, YearCalendar] = {}
        for p in sorted(directory.glob("krx-*.toml")):
            stem = p.stem.removeprefix("krx-")
            if not stem.isdigit():
                continue  # 예제(krx-2026.example.toml 등)는 읽지 않는다
            with p.open("rb") as f:
                years[int(stem)] = parse_calendar(tomllib.load(f), int(stem))
        return cls(years)

    def session(self, trade_date: date) -> SessionInfo | None:
        cal = self._years.get(trade_date.year)
        if cal is None or trade_date in self.conflicts:
            return None
        if trade_date.weekday() >= 5 or trade_date in cal.holidays:
            return SessionInfo(False)
        o, c = cal.special.get(trade_date, (KRX_REGULAR_OPEN, KRX_REGULAR_CLOSE))
        return SessionInfo(True, datetime.combine(trade_date, o, tzinfo=KST).astimezone(UTC),
                           datetime.combine(trade_date, c, tzinfo=KST).astimezone(UTC))

    def observe_live(self, trade_date: date, market_open: bool) -> bool:
        """실시간 장운영 통보로 본 개장 여부. 달력과 다르면 그날을 "모름"으로 두고 False(알림 필요)를 돌려준다."""
        expected = self.session(trade_date)
        if expected is None:
            return trade_date not in self.conflicts
        if expected.is_open != market_open:
            self.conflicts.append(trade_date)
            return False
        return True

    def is_trading_day(self, d: date) -> bool | None:
        s = self.session(d)
        return None if s is None else s.is_open
