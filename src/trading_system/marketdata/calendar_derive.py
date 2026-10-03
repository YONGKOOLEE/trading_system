"""거래일 목록에서 휴장일 달력 초안을 만든다 (A-037, 03 D-055, krx-market X-U3). 순수 함수 — 입출력 없음.

- 평일인데 거래일 목록에 없는 날 = 휴장일 후보. 주말은 달력이 자동으로 휴장으로 본다.
- 개장 시간 변경일(연초 개장·수능일)은 일봉으로 알 수 없다 → 공지로 따로 넣는다.
- 결과는 초안이다. 사람이 확인한 뒤 config/calendar에 반영하고, LIVE 전에 다시 확인한다(A-037 조건).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping, Sequence
from datetime import date, time, timedelta


def weekday_holidays(trading_days: Iterable[date], start: date, end: date) -> list[date]:
    days = set(trading_days)
    out: list[date] = []
    d = start
    while d <= end:
        if d.weekday() < 5 and d not in days:
            out.append(d)
        d += timedelta(days=1)
    return out


def render_calendar_toml(year: int, holidays: Sequence[date], special: Mapping[date, tuple[time, time]],
                         source: str, source_version: str, notes: Sequence[str]) -> str:
    def q(s: str) -> str:
        return '"' + s.replace("\\", "\\\\").replace('"', '\\"') + '"'

    lines = [f"# {n}" for n in notes]
    lines += ['market = "KRX"', f"year = {year}", f"source = {q(source)}", f"source_version = {q(source_version)}"]
    lines.append("holidays = [" + ", ".join(d.isoformat() for d in sorted(holidays)) + "]")
    for d in sorted(special):
        o, c = special[d]
        lines += ["", "[[special_sessions]]", f"date = {d.isoformat()}", f'open = "{o:%H:%M}"', f'close = "{c:%H:%M}"']
    return "\n".join(lines) + "\n"
