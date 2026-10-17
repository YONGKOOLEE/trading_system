"""A-108 전 종목 목록 도구 — 네트워크 없이 가짜 ka10099 응답으로만 시험한다.

열·종류 판정 규칙·4개 시장·codes-stocks.txt·비밀 없음·run.lock/하트비트 거부·허용 API 목록.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_system.app import list_stocks
from trading_system.app.daily_fetch import read_codes
from trading_system.app.instance_lock import acquire_instance_lock
from trading_system.app.list_stocks import classify, list_main, listed_date
from trading_system.app.preflight import GuardedHttp
from trading_system.broker.base import BrokerQueryError
from trading_system.broker.kiwoom.http import PreSendError
from trading_system.domain.clock import FixedClock

T0 = datetime(2026, 10, 17, 12, 0, tzinfo=UTC)
SECRET = "SECRET-APPKEY-0123456789"
COLUMNS = ["code", "name", "market", "listed_date", "kind", "kind_basis", "managed", "state", "audit_info",
           "order_warning"]


def row(code: str, name: str, *, state: str = "증거금20%", audit: str = "정상", reg: str | None = "20000104",
        up: str = "", warn: str = "0") -> dict:
    r = {"code": code, "name": name, "state": state, "auditInfo": audit, "orderWarning": warn, "marketName": "거래소",
         "upName": up}
    if reg is not None:
        r["regDay"] = reg
    return r


MASTER = {
    "0": [row("005930", "삼성전자"), row("005935", "삼성전자우"), row("00104K", "두산퓨얼셀1"),
          row("123450", "OO스팩1호"), row("088980", "맥쿼리인프라"), row("330590", "롯데리츠"),
          row("111110", "관리회사", state="관리종목", audit="관리종목"), row("069500", "KODEX 200"),
          row("999999", "날짜없음", reg=None)],
    "10": [row("247540", "에코프로비엠", reg="20190315"), row("035720", "카카오", audit="투자주의")],
    "8": [row("069500", "KODEX 200", reg="20021014"), row("114800", "KODEX 인버스")],
    "60": [row("580001", "ETN 예시")],
}


class FakeAdapter:
    def __init__(self, master: dict[str, list[dict]] | None = None, fail: set[str] | None = None) -> None:
        self.master = MASTER if master is None else master
        self.fail = fail or set()
        self.calls: list[tuple[str, dict]] = []
        self.order_calls: list[str] = []
        self.closed = False
        self.critical: list = []

    def query_raw(self, path, api_id, body, list_key, enough=None):  # type: ignore[no-untyped-def]
        self.calls.append((api_id, dict(body)))
        assert api_id == "ka10099" and list_key == "list"
        if body["mrkt_tp"] in self.fail:
            raise BrokerQueryError("ka10099: 코드 1700 (RATE_LIMIT)")
        return {}, self.master.get(body["mrkt_tp"], [])

    def place_order(self, *a, **k):  # type: ignore[no-untyped-def]
        self.order_calls.append("place_order")
        raise AssertionError("주문 경로 호출")

    def close(self) -> bool:
        self.closed = True
        return True


class FakeGuard:
    def __init__(self) -> None:
        self.blocked: list[str] = []
        self.records: list = []


class FakeConn:
    def __init__(self, adapter: FakeAdapter) -> None:
        self.adapter, self.guard = adapter, FakeGuard()


def run(tmp_path: Path, argv: list[str], fake: FakeAdapter | None = None) -> int:
    fake = fake or FakeAdapter()
    return list_main(tmp_path, argv, {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0),
                     connect=lambda root, env, clk: FakeConn(fake))


def args(tmp_path: Path) -> list[str]:
    return ["--mode", "PAPER", "--out", str(tmp_path / "o" / "stocks.csv")]


def table(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


def test_columns_markets_and_kinds(tmp_path: Path) -> None:
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path), fake) == 0
    rows = {r["code"]: r for r in table(tmp_path / "o" / "stocks.csv")}
    assert list(next(iter(rows.values())).keys()) == COLUMNS
    assert sorted(b["mrkt_tp"] for _, b in fake.calls) == ["0", "10", "60", "8"]
    assert {r["market"] for r in rows.values()} == {"KOSPI", "KOSDAQ", "ETF", "ETN"}
    assert rows["005930"]["kind"] == "보통주" and "추정" in rows["005930"]["kind_basis"]
    assert rows["005935"]["kind"] == "우선주" and "이름" in rows["005935"]["kind_basis"]
    assert rows["00104K"]["kind"] == "우선주" and "코드 끝자리" in rows["00104K"]["kind_basis"]
    assert rows["123450"]["kind"] == "스팩" and "스팩" in rows["123450"]["kind_basis"]
    assert rows["330590"]["kind"] == "리츠" and "리츠" in rows["330590"]["kind_basis"]
    assert rows["069500"]["market"] == "ETF" and rows["069500"]["kind"] == "ETF"      # 주식 목록에 섞여 있어도 ETF
    assert rows["069500"]["kind_basis"] == "시장코드" and rows["069500"]["listed_date"] == "2002-10-14"
    assert rows["580001"]["kind"] == "ETN" and rows["580001"]["market"] == "ETN"
    assert rows["111110"]["managed"] == "관리종목" and rows["005930"]["managed"] == ""
    assert rows["035720"]["audit_info"] == "투자주의" and rows["035720"]["managed"] == ""
    assert rows["247540"]["listed_date"] == "2019-03-15" and rows["005930"]["listed_date"] == "2000-01-04"
    assert rows["999999"]["listed_date"] == ""                                       # 필드 없으면 빈칸
    assert rows["005930"]["order_warning"] == "0" and rows["005930"]["state"] == "증거금20%"
    assert len(rows) == len(table(tmp_path / "o" / "stocks.csv"))                    # 코드 중복 없음
    assert fake.closed and fake.order_calls == []


def test_codes_stocks_file_excludes_etf_etn_and_works_with_fetch_daily(tmp_path: Path) -> None:
    assert run(tmp_path, args(tmp_path)) == 0
    p = tmp_path / "o" / "stocks.csv.codes-stocks.txt"
    codes = read_codes(p)
    assert codes == ["005930", "005935", "00104K", "123450", "088980", "330590", "111110", "999999", "247540", "035720"]
    assert not {"069500", "114800", "580001"} & set(codes)


@pytest.mark.parametrize("value, expected", [("20190315", "2019-03-15"), ("", ""), ("0", ""), ("00000000", ""),
                                             ("2019-03-15", ""), (None, "")])
def test_listed_date_parse(value: object, expected: str) -> None:
    assert listed_date({"regDay": value}) == expected
    assert listed_date({}) == ""


def test_classify_order_and_unknown_code() -> None:
    assert classify("005930", "삼성전자", "KOSPI", {})[0] == "보통주"
    assert classify("123450", "스팩우", "KOSDAQ", {})[0] == "스팩"          # 스팩이 우선주 규칙보다 먼저
    assert classify("12", "이상한", "KOSPI", {})[0] == "기타"
    assert classify("333333", "인프라펀드", "KOSPI", {"upName": "부동산투자회사"})[1] == "업종 '리츠'(추정)"


def test_all_dates_missing_is_reported(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    master = {k: [{x: y for x, y in r.items() if x != "regDay"} for r in v] for k, v in MASTER.items()}
    assert run(tmp_path, args(tmp_path), FakeAdapter(master)) == 0
    assert "regDay 필드 없음" in capsys.readouterr().out
    assert {r["listed_date"] for r in table(tmp_path / "o" / "stocks.csv")} == {""}


@pytest.mark.parametrize("argv", [["--mode", "LIVE"], ["--mode", "BACKTEST"], ["--mode", "paper"], []])
def test_refuses_non_paper_mode(tmp_path: Path, argv: list[str]) -> None:
    fake = FakeAdapter()
    extra = ["--out", str(tmp_path / "s.csv")] if argv else []
    assert run(tmp_path, argv + extra, fake) == 2
    assert fake.calls == [] and not fake.closed


def test_refuses_while_run_lock_held(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    lock = acquire_instance_lock(tmp_path / "var" / "paper")
    try:
        fake = FakeAdapter()
        assert run(tmp_path, args(tmp_path), fake) == 2
    finally:
        lock.release()
    assert fake.calls == [] and not (tmp_path / "o").exists()
    assert "실행 중" in capsys.readouterr().out


def test_refuses_while_heartbeat_fresh_and_old_one_passes(tmp_path: Path) -> None:
    hb = tmp_path / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((T0 - timedelta(seconds=10)).isoformat(), encoding="utf-8")
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path), fake) == 2 and fake.calls == []
    hb.write_text((T0 - timedelta(seconds=120)).isoformat(), encoding="utf-8")
    assert run(tmp_path, args(tmp_path), fake) == 0


def test_refuses_to_overwrite_and_connection_refusal(tmp_path: Path) -> None:
    (tmp_path / "o").mkdir()
    (tmp_path / "o" / "stocks.csv.codes-stocks.txt").write_text("keep\n", encoding="utf-8")
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path), fake) == 2 and fake.calls == []
    assert (tmp_path / "o" / "stocks.csv.codes-stocks.txt").read_text(encoding="utf-8") == "keep\n"
    (tmp_path / "o" / "stocks.csv.codes-stocks.txt").unlink()
    assert list_main(tmp_path, args(tmp_path), {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0),
                     connect=lambda r, e, c: "connection_approved = true가 아니다") == 2


@pytest.mark.parametrize("bad", ["0", "10", "8", "60"])
def test_query_failure_writes_nothing_and_revokes_token(tmp_path: Path, bad: str) -> None:
    fake = FakeAdapter(fail={bad})
    assert run(tmp_path, args(tmp_path), fake) == 1
    assert fake.closed and not (tmp_path / "o").exists()


def test_empty_stock_market_not_written(tmp_path: Path) -> None:
    fake = FakeAdapter({**MASTER, "10": []})
    assert run(tmp_path, args(tmp_path), fake) == 1 and not (tmp_path / "o").exists()


def test_default_connect_uses_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_connect(root, environ, clock, **kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return "거부(시험)"
    monkeypatch.setattr(list_stocks, "connect_paper", fake_connect)
    assert list_main(tmp_path, args(tmp_path), {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0)) == 2
    assert seen["allowed_apis"] == frozenset({"au10001", "au10002", "ka10099"})
    assert not (seen["allowed_apis"] & list_stocks.ORDER_API_IDS)


@pytest.mark.parametrize("api_id", ["ka10001", "ka10081", "ka20002", "ka20006", "kt10000", "kt10001", "kt10002", "kt10003"])
def test_guarded_http_blocks_every_other_api(api_id: str) -> None:
    class Inner:
        calls = 0

        def post(self, path, api, body, token, cont_yn=None, next_key=None):  # type: ignore[no-untyped-def]
            raise AssertionError("전송되면 안 됨")
    guard = GuardedHttp(Inner(), list_stocks.LIST_APIS)  # type: ignore[arg-type]
    with pytest.raises(PreSendError):
        guard.post("/api/dostk/x", api_id, {}, "tok")
    assert guard.blocked == [api_id]


def test_blocked_order_api_attempt_discards_results(tmp_path: Path) -> None:
    fake = FakeAdapter()
    conn = FakeConn(fake)
    conn.guard.blocked.append("kt10000")
    assert list_main(tmp_path, args(tmp_path), {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0),
                     connect=lambda r, e, c: conn) == 1
    assert fake.closed and not (tmp_path / "o").exists()


def test_outputs_and_console_contain_no_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    master = {k: [{**r, "appkey": SECRET} for r in v] for k, v in MASTER.items()}
    assert run(tmp_path, args(tmp_path), FakeAdapter(master)) == 0
    text = capsys.readouterr().out
    for f in (tmp_path / "o").iterdir():
        text += f.read_text(encoding="utf-8")
    assert SECRET not in text
    assert sorted(p.name for p in (tmp_path / "o").iterdir()) == ["stocks.csv", "stocks.csv.codes-stocks.txt"]
