"""휴장일 달력 자동 추출 (A-037, 03 D-055, krx-market X-U3) — 네트워크 없이, 합성 응답.

- 기준 종목 두 개(삼성전자·SK하이닉스) **모두** 일봉이 없는 평일만 휴장일로 본다(한 종목 거래정지를 휴장으로 오판 방지).
- 결과는 초안(var/paper/calendar-derive/)으로만 쓴다. config/calendar는 사람이 확인한 뒤 반영한다.
- 조회만 한다(주문 API 0건).
"""

from __future__ import annotations

import json
import tomllib
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.scenario.test_t015_preflight import ENV_OK, make_root
from tests.support.kiwoom_sim import Sim, _md_init
from trading_system.app.calendar_tool import DERIVE_CODES, derive_main
from trading_system.app.preflight import ORDER_API_IDS
from trading_system.domain.clock import KST, FixedClock
from trading_system.marketdata.calendar import parse_calendar
from trading_system.marketdata.calendar_derive import render_calendar_toml, weekday_holidays

T0 = datetime(2026, 10, 3, 3, 0, tzinfo=UTC)   # 2026-10-03(토) 12:00 KST
HOLIDAYS_2025 = [date(2025, 1, 1), date(2025, 3, 3), date(2025, 5, 5), date(2025, 12, 25)]
HALT_ONLY_A = date(2025, 4, 1)                 # 한 종목만 봉이 없는 날 (거래정지 가정) → 휴장 아님


def weekdays(a: date, b: date) -> Iterator[date]:
    d = a
    while d <= b:
        if d.weekday() < 5:
            yield d
        d += timedelta(days=1)


def rows(days: list[date]) -> list[dict]:
    out = []
    for i, d in enumerate(sorted(days, reverse=True)):
        p = 70000 + i
        out.append({"dt": d.strftime("%Y%m%d"), "open_pric": str(p), "high_pric": str(p + 100),
                    "low_pric": str(p - 100), "cur_prc": str(p), "trde_qty": "1", "pred_pre": "0"})
    return out


# --- 순수 함수 -----------------------------------------------------------------------------------

def test_weekday_holidays_are_weekdays_without_bars() -> None:
    days = [d for d in weekdays(date(2025, 1, 1), date(2025, 1, 10)) if d != date(2025, 1, 1)]
    assert weekday_holidays(days, date(2025, 1, 1), date(2025, 1, 10)) == [date(2025, 1, 1)]


def test_rendered_toml_parses_with_project_loader() -> None:
    text = render_calendar_toml(2025, [date(2025, 1, 1)], {}, "키움 ka10081 일봉 추출", "2026-10-03",
                                ["초안 — LIVE 전 재확인 (A-037)"])
    cal = parse_calendar(tomllib.loads(text), 2025)
    assert cal.holidays == frozenset({date(2025, 1, 1)})
    assert "초안" in text


def test_special_session_rendered() -> None:
    from datetime import time
    text = render_calendar_toml(2026, [], {date(2026, 11, 19): (time(10, 0), time(16, 30))}, "s", "v", [])
    assert parse_calendar(tomllib.loads(text), 2026).special[date(2026, 11, 19)] == (time(10, 0), time(16, 30))


# --- 진입점 (합성 응답) ---------------------------------------------------------------------------

@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def sim(clock: FixedClock) -> Iterator[Sim]:
    s = Sim(now=lambda: clock.now().astimezone(KST).replace(tzinfo=None))
    _md_init(s)
    all_days = [d for d in weekdays(date(2023, 12, 1), date(2026, 10, 2)) if d not in HOLIDAYS_2025]
    s.daily[DERIVE_CODES[0]] = rows([d for d in all_days if d != HALT_ONLY_A])
    s.daily[DERIVE_CODES[1]] = rows(all_days)
    s.page_size = 300
    yield s
    s.close()


def test_derive_writes_drafts_and_sends_no_order(tmp_path: Path, sim: Sim, clock: FixedClock,
                                                 capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    code = derive_main(root, {"TRADE_MODE": "PAPER"}, years=(2024, 2025, 2026), rest_url=sim.url,
                       ws_url=sim.ws_url, clock=clock, per_tr_interval=0.0)
    out = capsys.readouterr().out
    assert code == 0, out
    draft = root / "var" / "paper" / "calendar-derive"
    cal25 = parse_calendar(tomllib.loads((draft / "krx-2025.draft.toml").read_text(encoding="utf-8")), 2025)
    assert sorted(cal25.holidays) == HOLIDAYS_2025          # 한 종목만 빠진 날은 휴장 아님
    assert HALT_ONLY_A not in cal25.holidays
    summary = json.loads((draft / "summary.json").read_text(encoding="utf-8"))
    assert summary["2026"]["covered_until"] == "2026-10-02"  # 올해는 마지막 확정 거래일까지만
    assert summary["2025"]["single_code_gaps"] == ["2025-04-01"]
    assert not list((root / "config" / "calendar").glob("krx-*.toml"))   # config에는 쓰지 않는다
    assert not {api for api, _, _ in sim.log} & ORDER_API_IDS


def test_derive_refuses_without_connection_switch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=False)
    assert derive_main(root, {"TRADE_MODE": "PAPER"}) == 2
    assert "connection_approved" in capsys.readouterr().out
