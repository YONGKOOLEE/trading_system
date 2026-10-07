"""T-018 백테스트용 일봉 수집 도구 (A-073 E·F) — 네트워크 없이 가짜 어댑터 응답으로만 시험한다.

- `--mode PAPER`만 허용, 본 프로세스(run)가 실행 중이면(하트비트 30초 이내) 연결 전에 거부.
- 종목 선정: ka10099 코스피 목록에서 ETF·ETN·ELW·우선주·투자유의·거래정지 제외 → ka10001 시가총액 순위 → 상위 N.
- 일봉: ka10081 수정주가(upd_stkpc_tp=1)를 since부터 → `<out>/<코드>.csv`(date,open,high,low,close,volume).
- 실패 종목은 건너뛰고 기록, --dry-run은 일봉 조회 0회, 주문 경로 호출 0회, 끝나면 토큰 폐기.
"""

from __future__ import annotations

import csv
import json
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from trading_system.app.daily_fetch import exclusion_reason, fetch_main, is_preferred
from trading_system.app.main import load_daily_csv
from trading_system.broker.base import BrokerQueryError
from trading_system.domain.clock import FixedClock

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)    # 2026-10-07(수) 21:00 KST — 당일 일봉 확정(20:00) 뒤
UNTIL = date(2026, 10, 7)


def master_row(code: str, name: str, *, state: str = "증거금20%|담보대출|신용가능", audit: str = "정상",
               warn: str = "0", market: str = "거래소") -> dict:
    return {"code": code, "name": name, "state": state, "auditInfo": audit, "orderWarning": warn,
            "marketName": market}


def daily_rows(first: date, last: date, base: int) -> list[dict]:
    out, d, i = [], last, 0
    while d >= first:
        if d.weekday() < 5:
            p = base + i
            out.append({"dt": d.strftime("%Y%m%d"), "open_pric": f"+{p}", "high_pric": str(p + 100),
                        "low_pric": f"-{p - 100}", "cur_prc": str(p), "trde_qty": str(1000 + i)})
            i += 1
        d -= timedelta(days=1)
    return out   # 키움처럼 최신 → 과거 순


class FakeAdapter:
    """KiwoomAdapter의 조회 경로(query_raw)·close 모양만 흉내 낸다. 주문 메서드가 불리면 기록한다."""

    def __init__(self, kospi: list[dict], caps: dict[str, dict], daily: dict[str, list[dict]],
                 etf: list[dict] | None = None, etn: list[dict] | None = None,
                 fail_daily: set[str] | None = None) -> None:
        self.kospi, self.caps, self.daily = kospi, caps, daily
        self.etf, self.etn = etf or [], etn or []
        self.fail_daily = fail_daily or set()
        self.calls: list[tuple[str, dict]] = []
        self.order_calls: list[str] = []
        self.closed = False
        self.critical: list[tuple[str, str]] = []

    def query_raw(self, path: str, api_id: str, body: dict, list_key: str | None, enough=None):  # type: ignore[no-untyped-def]
        self.calls.append((api_id, dict(body)))
        if api_id == "ka10099":
            assert list_key == "list"
            return {}, {"0": self.kospi, "8": self.etf, "60": self.etn}[body["mrkt_tp"]]
        if api_id == "ka10001":
            if body["stk_cd"] not in self.caps:
                raise BrokerQueryError("ka10001: 코드 1999")
            return self.caps[body["stk_cd"]], []
        if api_id == "ka10081":
            assert body["upd_stkpc_tp"] == "1" and list_key == "stk_dt_pole_chart_qry"
            if body["stk_cd"] in self.fail_daily:
                raise BrokerQueryError("ka10081: 코드 1700 (RATE_LIMIT)")
            rows: list[dict] = []
            for r in self.daily.get(body["stk_cd"], []):   # 쪽 단위(100행)로 넘기며 enough 확인
                rows.append(r)
                if enough is not None and len(rows) % 100 == 0 and enough(rows):
                    break
            return {}, rows
        raise AssertionError(f"예상 밖 API {api_id}")

    def place_order(self, *a, **k):  # type: ignore[no-untyped-def]
        self.order_calls.append("place_order")
        raise AssertionError("주문 경로 호출")

    def cancel_order(self, *a, **k):  # type: ignore[no-untyped-def]
        self.order_calls.append("cancel_order")
        raise AssertionError("주문 경로 호출")

    def modify_order(self, *a, **k):  # type: ignore[no-untyped-def]
        self.order_calls.append("modify_order")
        raise AssertionError("주문 경로 호출")

    def close(self) -> bool:
        self.closed = True
        return True

    def count(self, api_id: str) -> int:
        return sum(1 for a, _ in self.calls if a == api_id)


class FakeGuard:
    def __init__(self) -> None:
        self.blocked: list[str] = []
        self.records: list = []


class FakeConn:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter, self.guard = adapter, FakeGuard()


def cap(mac: int, shares: int = 1000, price: int = 1000) -> dict:
    return {"return_code": 0, "mac": str(mac), "flo_stk": str(shares), "cur_prc": f"+{price}"}


KOSPI = [
    master_row("005930", "삼성전자"),
    master_row("000660", "SK하이닉스"),
    master_row("005935", "삼성전자우"),                     # 우선주(이름·코드)
    master_row("005387", "현대차2우B"),                     # 우선주
    master_row("069500", "KODEX 200"),                      # ETF 목록(8)에 있음
    master_row("111110", "정지종목", state="거래정지"),
    master_row("222220", "감사정지", audit="거래정지"),
    master_row("333330", "경고종목", audit="투자경고"),
    master_row("444440", "정리매매", warn="2"),
    master_row("555550", "ELW상품", market="ELW"),
    master_row("373220", "LG에너지솔루션"),
    master_row("207940", "삼성바이오로직스"),
    master_row("005380", "현대차"),
]
CAPS = {"005930": cap(16000000, 5919637, 270000), "000660": cap(9000000, 728002, 1236000),
        "373220": cap(800000, 234000, 342000), "207940": cap(700000, 71174, 984000),
        "005380": cap(500000, 204757, 244000),
        # 제외 대상도 시가총액은 크게 둔다 — 제외 규칙이 순위보다 먼저인지 확인
        "005935": cap(99000000), "005387": cap(99000000), "069500": cap(99000000), "111110": cap(99000000),
        "222220": cap(99000000), "333330": cap(99000000), "444440": cap(99000000), "555550": cap(99000000)}
ETF = [master_row("069500", "KODEX 200")]


def make_fake(**kw) -> FakeAdapter:  # type: ignore[no-untyped-def]
    daily = {c: daily_rows(date(2015, 11, 2), UNTIL + timedelta(days=0), 50000 + i * 1000)
             for i, c in enumerate(["005930", "000660", "373220", "207940", "005380"])}
    daily["373220"] = daily_rows(date(2022, 1, 27), UNTIL, 400000)   # 상장이 since 이후인 종목
    return FakeAdapter(kw.pop("kospi", KOSPI), kw.pop("caps", CAPS), kw.pop("daily", daily), etf=ETF, **kw)


def run(tmp_path: Path, argv: list[str], fake: FakeAdapter, *, clock: FixedClock | None = None) -> int:
    def connect(root, environ, clk):  # type: ignore[no-untyped-def]
        return FakeConn(fake)
    return fetch_main(tmp_path, argv, {"TRADE_MODE": "PAPER"}, clock=clock or FixedClock(T0), connect=connect)


TOP3 = ["--mode", "PAPER", "--top", "20"]


# --- 순수 함수 -------------------------------------------------------------------------------------

@pytest.mark.parametrize("code, name, expected", [
    ("005935", "삼성전자우", True), ("005387", "현대차2우B", True), ("003555", "LG우", True),
    ("051915", "LG화학우", True), ("005930", "삼성전자", False), ("000660", "SK하이닉스", False),
    ("010950", "S-Oil", False), ("004020", "현대제철", False),
])
def test_preferred_rule(code: str, name: str, expected: bool) -> None:
    assert is_preferred(code, name) is expected


def test_exclusion_rules() -> None:
    etf = {"069500"}
    by_code = {r["code"]: r for r in KOSPI}
    assert exclusion_reason(by_code["005930"], etf) is None
    assert "ETF" in (exclusion_reason(by_code["069500"], etf) or "")
    assert "우선주" in (exclusion_reason(by_code["005935"], etf) or "")
    for c in ("111110", "222220"):
        assert "거래정지" in (exclusion_reason(by_code[c], etf) or "")
    for c in ("333330", "444440"):
        assert "투자유의" in (exclusion_reason(by_code[c], etf) or "")
    assert "ELW" in (exclusion_reason(by_code["555550"], etf) or "")


# --- 진입점 거부 ------------------------------------------------------------------------------------

@pytest.mark.parametrize("argv", [["--mode", "LIVE"], ["--mode", "BACKTEST"], ["--mode", "paper"], []])
def test_refuses_non_paper_mode(tmp_path: Path, argv: list[str], capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_fake()
    assert run(tmp_path, argv, fake) == 2
    assert fake.calls == [] and not fake.closed


@pytest.mark.parametrize("top", ["19", "51", "abc"])
def test_refuses_top_out_of_range(tmp_path: Path, top: str) -> None:
    fake = make_fake()
    assert run(tmp_path, ["--mode", "PAPER", "--top", top], fake) == 2
    assert fake.calls == []


def test_refuses_while_run_heartbeat_fresh(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    hb = tmp_path / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((T0 - timedelta(seconds=10)).isoformat(), encoding="utf-8")
    fake = make_fake()
    assert run(tmp_path, TOP3, fake) == 2
    assert fake.calls == []
    assert "하트비트" in capsys.readouterr().out


def test_old_heartbeat_does_not_block(tmp_path: Path) -> None:
    hb = tmp_path / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((T0 - timedelta(seconds=120)).isoformat(), encoding="utf-8")
    assert run(tmp_path, TOP3 + ["--dry-run"], make_fake()) == 0


def test_refuses_when_connection_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    code = fetch_main(tmp_path, TOP3, {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0),
                      connect=lambda root, env, clk: "connection_approved = true가 아니다")
    assert code == 2
    assert "connection_approved" in capsys.readouterr().out


def test_refuses_existing_csv_in_out_dir(tmp_path: Path) -> None:
    out = tmp_path / "data"
    out.mkdir()
    (out / "005930.csv").write_text("date,open,high,low,close,volume\n", encoding="utf-8")
    fake = make_fake()
    assert run(tmp_path, TOP3 + ["--out", str(out)], fake) == 2
    assert fake.calls == []


# --- 종목 선정 --------------------------------------------------------------------------------------

def test_dry_run_selects_without_daily_calls(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_fake()
    out = tmp_path / "data"
    assert run(tmp_path, TOP3 + ["--dry-run", "--out", str(out)], fake) == 0
    assert fake.count("ka10081") == 0
    assert fake.count("ka10001") == 5          # 제외 종목은 시가총액 조회도 안 함
    assert fake.order_calls == [] and fake.closed
    with (out / "meta" / "universe.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert [r["code"] for r in rows] == ["005930", "000660", "373220", "207940", "005380"]
    assert [r["rank"] for r in rows] == ["1", "2", "3", "4", "5"]
    assert rows[0]["name"] == "삼성전자" and rows[0]["market_cap"] == "16000000"
    assert "ka10001 mac" in rows[0]["basis"]
    assert not list(out.glob("*.csv"))         # 데이터 폴더 바로 아래에는 일봉 CSV만 (백테스트가 *.csv를 읽음)
    text = capsys.readouterr().out
    assert "ka10001" in text and "mac" in text and "미확인" in text


def test_top_n_limits_and_order(tmp_path: Path) -> None:
    kospi = [master_row(f"{100000 + i * 10:06d}", f"종목{i}") for i in range(40)]
    caps = {r["code"]: cap(1000 + i) for i, r in enumerate(kospi)}     # 뒤로 갈수록 큼
    fake = FakeAdapter(kospi, caps, {})
    out = tmp_path / "data"
    assert run(tmp_path, ["--mode", "PAPER", "--top", "25", "--dry-run", "--out", str(out)], fake) == 0
    with (out / "meta" / "universe.csv").open(encoding="utf-8", newline="") as f:
        rows = list(csv.DictReader(f))
    assert len(rows) == 25
    assert rows[0]["code"] == kospi[39]["code"] and rows[-1]["code"] == kospi[15]["code"]


def test_missing_market_cap_field_stops(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    caps = dict(CAPS)
    caps["000660"] = {"return_code": 0, "cur_prc": "1236000"}          # mac 없음 → 추측하지 않고 중단
    fake = make_fake(caps=caps)
    assert run(tmp_path, TOP3, fake) == 1
    assert fake.count("ka10081") == 0 and fake.closed
    assert "mac" in capsys.readouterr().out


def test_market_cap_query_failure_stops(tmp_path: Path) -> None:
    caps = {k: v for k, v in CAPS.items() if k != "207940"}             # 한 종목 ka10001 실패 → 순위 불완전
    fake = make_fake(caps=caps)
    assert run(tmp_path, TOP3, fake) == 1
    assert fake.count("ka10081") == 0 and fake.closed


# --- 일봉 수집 --------------------------------------------------------------------------------------

def test_fetch_writes_backtest_csv_and_skips_failures(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = make_fake(fail_daily={"207940"})
    out = tmp_path / "data"
    code = run(tmp_path, TOP3 + ["--out", str(out), "--since", "2016-01-01"], fake)
    text = capsys.readouterr().out
    assert code == 1, text                      # 실패 종목이 있으면 종료 코드 1 (나머지는 저장)
    assert fake.order_calls == [] and fake.closed
    assert sorted(p.name for p in out.glob("*.csv")) == ["000660.csv", "005380.csv", "005930.csv", "373220.csv"]
    with (out / "005930.csv").open(encoding="utf-8", newline="") as f:
        lines = f.read().splitlines()
    assert lines[0] == "date,open,high,low,close,volume"
    first = lines[1].split(",")
    assert first[0] == "2016-01-01" and all(v.isdigit() for v in first[1:])   # 부호 제거, 정수
    assert lines[-1].startswith("2026-10-07,")
    dates = [ln.split(",")[0] for ln in lines[1:]]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    bars = load_daily_csv(out / "005930.csv")    # 백테스트 로더로 그대로 읽힌다
    assert bars[0].day == date(2016, 1, 1)
    assert load_daily_csv(out / "373220.csv")[0].day == date(2022, 1, 27)
    summary = json.loads(next((out / "meta").glob("fetch-summary-*.json")).read_text(encoding="utf-8"))
    assert summary["ok"] == 4 and summary["failed"] == [{"code": "207940", "reason": "ka10081: 코드 1700 (RATE_LIMIT)"}]
    assert summary["order_api_calls"] == 0
    assert "성공 4" in text and "실패 1" in text


def test_daily_query_parameters(tmp_path: Path) -> None:
    fake = make_fake()
    out = tmp_path / "data"
    assert run(tmp_path, TOP3 + ["--out", str(out)], fake) == 0
    bodies = [b for a, b in fake.calls if a == "ka10081"]
    assert {b["base_dt"] for b in bodies} == {"20261007"}
    assert {b["upd_stkpc_tp"] for b in bodies} == {"1"}
    assert fake.order_calls == []


def test_incomplete_today_bar_excluded_before_20(tmp_path: Path) -> None:
    clock = FixedClock(datetime(2026, 10, 7, 9, 0, tzinfo=UTC))     # 18:00 KST — 당일 봉 미확정
    fake = make_fake()
    out = tmp_path / "data"
    assert run(tmp_path, TOP3 + ["--out", str(out)], fake, clock=clock) == 0
    last = (out / "005930.csv").read_text(encoding="utf-8").splitlines()[-1]
    assert last.startswith("2026-10-06,")
    assert {b["base_dt"] for a, b in fake.calls if a == "ka10081"} == {"20261006"}


def test_default_out_dir_uses_last_complete_day(tmp_path: Path) -> None:
    assert run(tmp_path, TOP3 + ["--dry-run"], make_fake()) == 0
    assert (tmp_path / "var" / "backtest" / "data" / "kiwoom-ka10081-adj-20261007" / "meta" / "universe.csv").exists()


def test_duplicate_daily_date_fails_that_code_only(tmp_path: Path) -> None:
    fake = make_fake()
    fake.daily["005380"] = fake.daily["005380"] + [dict(fake.daily["005380"][0])]
    out = tmp_path / "data"
    assert run(tmp_path, TOP3 + ["--out", str(out)], fake) == 1
    assert not (out / "005380.csv").exists() and (out / "005930.csv").exists()


def test_codes_file_skips_ranking(tmp_path: Path) -> None:
    codes = tmp_path / "codes.txt"
    codes.write_text("# 직접 지정\n005930\n000660\n", encoding="utf-8")
    fake = make_fake()
    out = tmp_path / "data"
    assert run(tmp_path, ["--mode", "PAPER", "--codes", str(codes), "--out", str(out)], fake) == 0
    assert fake.count("ka10099") == 0 and fake.count("ka10001") == 0
    assert sorted(p.name for p in out.glob("*.csv")) == ["000660.csv", "005930.csv"]


def test_order_api_attempt_discards_result(tmp_path: Path) -> None:
    fake = make_fake()
    conn = FakeConn(fake)
    conn.guard.blocked.append("kt10000")       # 어떤 경로로든 주문 API 시도가 있었다면 실패 처리
    out = tmp_path / "data"
    code = fetch_main(tmp_path, TOP3 + ["--out", str(out)], {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0),
                      connect=lambda root, env, clk: conn)
    assert code == 1


def test_codes_retry_into_same_folder(tmp_path: Path) -> None:
    """실패 종목 재수집: 같은 폴더에 다른 종목 CSV가 있어도 --codes 종목 파일만 없으면 허용. universe.csv도 입력으로 받음."""
    out = tmp_path / "data"
    assert run(tmp_path, TOP3 + ["--out", str(out)], make_fake(fail_daily={"207940"})) == 1
    universe = (out / "meta" / "universe.csv").read_text(encoding="utf-8")
    retry = tmp_path / "retry.txt"
    retry.write_text("207940" + chr(10), encoding="utf-8")
    fake = make_fake()
    assert run(tmp_path, ["--mode", "PAPER", "--codes", str(retry), "--out", str(out)], fake) == 0
    assert (out / "207940.csv").exists() and fake.count("ka10081") == 1
    assert (out / "meta" / "universe.csv").read_text(encoding="utf-8") == universe   # 순위 근거 보존
    # 이미 있는 종목을 다시 지정하면 거부(덮어쓰지 않음)
    fake2 = make_fake()
    assert run(tmp_path, ["--mode", "PAPER", "--codes", str(out / "meta" / "universe.csv"), "--out", str(out)],
               fake2) == 2
    assert fake2.calls == []


def test_universe_csv_accepted_as_codes(tmp_path: Path) -> None:
    out = tmp_path / "sel"
    assert run(tmp_path, TOP3 + ["--dry-run", "--out", str(out)], make_fake()) == 0
    fake = make_fake()
    out2 = tmp_path / "data"
    assert run(tmp_path, ["--mode", "PAPER", "--codes", str(out / "meta" / "universe.csv"), "--out", str(out2)],
               fake) == 0
    assert fake.count("ka10099") == 0 and fake.count("ka10001") == 0
    assert sorted(p.stem for p in out2.glob("*.csv")) == sorted(["005930", "000660", "373220", "207940", "005380"])


def test_default_connect_refuses_without_trade_mode(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """기본 연결(connect_paper)도 TRADE_MODE가 없으면 네트워크 객체를 만들기 전에 거부한다."""
    assert fetch_main(tmp_path, TOP3, {}, clock=FixedClock(T0)) == 2
    assert "TRADE_MODE" in capsys.readouterr().out


def test_codes_file_with_bom(tmp_path: Path) -> None:
    codes = tmp_path / "codes.txt"
    codes.write_bytes(chr(0xFEFF).encode("utf-8") + b"005930\r\n")   # PowerShell 5.1 Set-Content -Encoding utf8
    fake = make_fake()
    assert run(tmp_path, ["--mode", "PAPER", "--codes", str(codes), "--out", str(tmp_path / "d")], fake) == 0
    assert fake.count("ka10081") == 1
