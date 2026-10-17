"""A-108 fetch-daily 추가 옵션 — 네트워크 없이 가짜 어댑터로만 시험한다.

--skip-existing(이어받기·지수) · --deadline(시한 중단·not_queried·종료 1) · --amount(있음/없음) ·
--unadjusted(upd_stkpc_tp=0, --out 필수) · 요약 JSON에 사용한 플래그 기록. 옵션이 없으면 기존 동작(6열·수정주가).
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trading_system.app.daily_fetch import fetch_main
from trading_system.domain.clock import FixedClock

UNTIL = date(2026, 10, 16)
T0 = datetime(2026, 10, 17, 3, 0, tzinfo=UTC)     # 2026-10-17(토) 12:00 KST
CODES = ["005930", "000660", "005380"]


def bars(first: date, last: date, base: int, amount: bool) -> list[dict]:
    out, d, i = [], last, 0
    while d >= first:
        if d.weekday() < 5:
            p = base + i
            r = {"dt": d.strftime("%Y%m%d"), "open_pric": f"+{p}", "high_pric": str(p + 100), "low_pric": f"-{p - 100}",
                 "cur_prc": str(p), "trde_qty": str(1000 + i)}
            if amount:
                r["trde_prica"] = str(5000 + i)
            out.append(r)
            i += 1
        d -= timedelta(days=1)
    return out


class Fake:
    def __init__(self, amount: bool = True, index: bool = True, hook=None) -> None:  # type: ignore[no-untyped-def]
        self.amount, self.index, self.hook = amount, index, hook
        self.calls: list[tuple[str, dict]] = []
        self.closed = False
        self.critical: list = []

    def query_raw(self, path, api_id, body, list_key, enough=None):  # type: ignore[no-untyped-def]
        self.calls.append((api_id, dict(body)))
        if api_id == "ka10081":
            if self.hook:
                self.hook(body["stk_cd"])
            return {}, bars(date(2025, 12, 1), UNTIL, 50000, self.amount)
        if api_id == "ka20006":
            return {}, [{"dt": r["dt"], "open_pric": "250000", "high_pric": "251000", "low_pric": "249000",
                         "cur_prc": "250500", "trde_qty": "9"} for r in bars(date(2025, 12, 1), UNTIL, 1, False)]
        raise AssertionError(api_id)

    def close(self) -> bool:
        self.closed = True
        return True

    def stk(self) -> list[str]:
        return [b["stk_cd"] for a, b in self.calls if a == "ka10081"]


class Guard:
    blocked: list = []
    records: list = []


class Conn:
    def __init__(self, a: Fake) -> None:
        self.adapter, self.guard = a, Guard()


class StepClock:
    """now()를 부를 때마다 step만큼 흐르는 시계(시한 시험용)."""

    def __init__(self, start: datetime, step: timedelta) -> None:
        self.t, self.step = start, step

    def now(self) -> datetime:
        v = self.t
        self.t += self.step
        return v


def codes_file(tmp: Path) -> str:
    p = tmp / "codes.txt"
    p.write_text("\n".join(CODES) + "\n", encoding="utf-8")
    return str(p)


def go(tmp: Path, extra: list[str], fake: Fake, clock=None) -> int:  # type: ignore[no-untyped-def]
    argv = ["--mode", "PAPER", "--since", "2026-01-01", "--codes", codes_file(tmp), *extra]
    return fetch_main(tmp, argv, {"TRADE_MODE": "PAPER"}, clock=clock or FixedClock(T0),
                      connect=lambda r, e, c: Conn(fake))


def summary(out: Path) -> dict:
    (p,) = sorted((out / "meta").glob("fetch-summary-*.json"))
    return json.loads(p.read_text(encoding="utf-8"))


def header(p: Path) -> list[str]:
    with p.open(encoding="utf-8", newline="") as f:
        return next(csv.reader(f))


def test_default_behaviour_unchanged(tmp_path: Path) -> None:
    out, fake = tmp_path / "o", Fake()
    assert go(tmp_path, ["--out", str(out)], fake) == 0
    assert header(out / "005930.csv") == ["date", "open", "high", "low", "close", "volume"]
    assert {b["upd_stkpc_tp"] for a, b in fake.calls if a == "ka10081"} == {"1"}
    s = summary(out)
    assert s["flags"] == {"skip_existing": False, "deadline": None, "amount": False, "unadjusted": False,
                          "index": None, "kospi200": False, "codes_file": True, "dry_run": False}
    assert "amount" not in s


def test_existing_without_skip_is_still_refused(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.mkdir()
    (out / "000660.csv").write_text("date,open,high,low,close,volume\n", encoding="utf-8")
    fake = Fake()
    assert go(tmp_path, ["--out", str(out)], fake) == 2 and fake.calls == []


def test_skip_existing_resumes_and_keeps_existing_file(tmp_path: Path) -> None:
    out = tmp_path / "o"
    out.mkdir()
    (out / "000660.csv").write_text("KEEP\n", encoding="utf-8")
    (out / "index").mkdir()
    (out / "index" / "001.csv").write_text("KEEP\n", encoding="utf-8")
    fake = Fake()
    assert go(tmp_path, ["--out", str(out), "--skip-existing", "--index", "001"], fake) == 0
    assert fake.stk() == ["005930", "005380"] and not any(a == "ka20006" for a, _ in fake.calls)
    assert (out / "000660.csv").read_text(encoding="utf-8") == "KEEP\n"
    assert (out / "index" / "001.csv").read_text(encoding="utf-8") == "KEEP\n"
    s = summary(out)
    assert s["skipped_existing"] == ["000660", "index/001"] and s["codes"] == ["005930", "005380"]
    assert s["flags"]["skip_existing"] is True and s["not_queried"] == [] and fake.closed
    # 두 번째 실행: 남은 것이 없어도 정상 종료
    fake2 = Fake()
    assert go(tmp_path, ["--out", str(out), "--skip-existing", "--index", "001"], fake2) == 0
    assert fake2.stk() == []


def test_deadline_stops_before_next_code_and_lists_not_queried(tmp_path: Path) -> None:
    out = tmp_path / "o"
    t = {"now": datetime(2026, 10, 17, 10, 50, tzinfo=UTC)}      # 19:50 KST
    clock = type("C", (), {"now": lambda self: t["now"]})()
    # 첫 종목을 조회하는 사이 시각이 20:00 KST가 된다
    fake = Fake(hook=lambda code: t.update(now=datetime(2026, 10, 17, 11, 0, tzinfo=UTC)) if code == "005930" else None)
    rc = go(tmp_path, ["--out", str(out), "--deadline", "20:00", "--index", "001"], fake, clock)
    assert rc == 1 and fake.stk() == ["005930"]
    assert (out / "005930.csv").exists() and not (out / "000660.csv").exists()
    s = summary(out)
    assert s["not_queried"] == ["000660", "005380", "index/001"] and s["deadline_reached"] is True
    assert s["flags"]["deadline"] == "20:00" and fake.closed


def test_deadline_not_reached_completes(tmp_path: Path) -> None:
    out, fake = tmp_path / "o", Fake()
    assert go(tmp_path, ["--out", str(out), "--deadline", "23:59"], fake) == 0   # 시작 12:00 KST
    assert summary(out)["not_queried"] == []


def test_deadline_already_passed_queries_nothing(tmp_path: Path) -> None:
    out, fake = tmp_path / "o", Fake()
    assert go(tmp_path, ["--out", str(out), "--deadline", "11:00"], fake) == 1   # 시작 12:00 KST
    assert fake.stk() == [] and summary(out)["not_queried"] == CODES and fake.closed


@pytest.mark.parametrize("bad", ["25:00", "abc", "9", "20:00:30"])
def test_deadline_bad_format_refused(tmp_path: Path, bad: str) -> None:
    fake = Fake()
    assert go(tmp_path, ["--out", str(tmp_path / "o"), "--deadline", bad], fake) == 2 and fake.calls == []


def test_amount_present(tmp_path: Path) -> None:
    out, fake = tmp_path / "o", Fake(amount=True)
    assert go(tmp_path, ["--out", str(out), "--amount"], fake) == 0
    assert header(out / "005930.csv") == ["date", "open", "high", "low", "close", "volume", "amount"]
    rows = list(csv.DictReader((out / "005930.csv").open(encoding="utf-8")))
    assert rows[-1]["amount"] == "5000" and all(r["amount"] for r in rows)
    a = summary(out)["amount"]
    assert a["field"] == "trde_prica" and a["rows_with_value"] == a["rows"] > 0 and "note" not in a


def test_amount_absent_writes_empty_column_and_notes(tmp_path: Path) -> None:
    out, fake = tmp_path / "o", Fake(amount=False)
    assert go(tmp_path, ["--out", str(out), "--amount"], fake) == 0
    rows = list(csv.DictReader((out / "005930.csv").open(encoding="utf-8")))
    assert header(out / "005930.csv")[-1] == "amount" and {r["amount"] for r in rows} == {""}
    a = summary(out)["amount"]
    assert a["rows_with_value"] == 0 and a["note"] == "거래대금 필드 없음"


def test_unadjusted_sets_upd_stkpc_tp_0(tmp_path: Path) -> None:
    out, fake = tmp_path / "o", Fake()
    assert go(tmp_path, ["--out", str(out), "--unadjusted"], fake) == 0
    assert {b["upd_stkpc_tp"] for a, b in fake.calls if a == "ka10081"} == {"0"}
    s = summary(out)
    assert "원주가" in s["api"] and s["flags"]["unadjusted"] is True


def test_unadjusted_requires_out(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = Fake()
    assert go(tmp_path, ["--unadjusted"], fake) == 2 and fake.calls == [] and not fake.closed
    assert "--out" in capsys.readouterr().out
