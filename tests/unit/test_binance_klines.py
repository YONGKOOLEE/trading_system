"""바이낸스 공개 klines 수집 도구 (A-110). 네트워크 없음 — 가짜 HTTP 계층만 쓴다."""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import importlib.util
import sys

import pytest


_SCRIPT = Path(__file__).resolve().parents[2] / "scripts" / "fetch-binance-klines.py"
_spec = importlib.util.spec_from_file_location("fetch_binance_klines", _SCRIPT)
bk = importlib.util.module_from_spec(_spec)  # type: ignore[arg-type]
sys.modules["fetch_binance_klines"] = bk
_spec.loader.exec_module(bk)  # type: ignore[union-attr]
RestAfterSendError, RestReply = bk.RestAfterSendError, bk.RestReply

STEP = 60_000
T0 = 1_700_000_000_000 - (1_700_000_000_000 % STEP)


class FakeServer:
    """1분봉 n개를 가진 가짜 서버. 요청을 기록한다."""

    def __init__(self, n: int, *, now_extra: int = 0, skip: set[int] | None = None, step: int = STEP) -> None:
        self.step = step
        idx = [i for i in range(n) if i not in (skip or set())]
        self.bars = [self._bar(T0 + i * step) for i in idx]
        last_close = T0 + (n - 1) * step + step - 1
        self.now = last_close + 1 - now_extra   # now_extra>0이면 마지막 봉이 아직 열려 있다
        self.requests: list[tuple[str, str, dict[str, str]]] = []
        self.script: list[RestReply | Exception] = []

    def _bar(self, o: int) -> list[object]:
        return [o, "0.10000000", "100.5", "99.50000000", "100.123456789", "1.00000000", o + self.step - 1,
                "100.12345678", 7, "0.5", "50.0", "0"]

    def __call__(self, method: str, url: str, headers: Mapping[str, str]) -> RestReply:
        p = urlsplit(url)
        q = {k: v[0] for k, v in parse_qs(p.query).items()}
        self.requests.append((method, url, dict(headers)))
        if self.script:
            item = self.script.pop(0)
            if isinstance(item, Exception):
                raise item
            return item
        if p.path == "/api/v3/time":
            return RestReply(200, {}, json.dumps({"serverTime": self.now}))
        if p.path == "/api/v3/exchangeInfo":
            return RestReply(200, {}, json.dumps({"symbols": [{"symbol": q["symbol"], "status": "TRADING"}]}))
        start, lim = int(q["startTime"]), int(q["limit"])
        end = int(q["endTime"]) if "endTime" in q else None
        rows = [b for b in self.bars if b[0] >= start and (end is None or b[0] <= end)][:lim]  # type: ignore[operator]
        return RestReply(200, {}, json.dumps(rows))

    def klines_calls(self) -> int:
        return sum(1 for _, u, _ in self.requests if urlsplit(u).path == "/api/v3/klines")


def run(tmp: Path, srv: FakeServer, *extra: str, since: str = "listing", interval: str = "1m",
        symbol: str = "BTCUSDT") -> tuple[int, list[str], list[float]]:
    out: list[str] = []
    sleeps: list[float] = []
    clock = [0.0]

    def sleep(s: float) -> None:
        sleeps.append(s)
        clock[0] += s

    rc = bk.klines_main(["--symbol", symbol, "--interval", interval, "--since", since, "--out", str(tmp), *extra],
                        http=srv, sleep=sleep, mono=lambda: clock[0], out=out.append)
    return rc, out, sleeps


def lines(tmp: Path) -> list[str]:
    return (tmp / "BTCUSDT-1m.csv").read_text(encoding="utf-8").splitlines()


def test_pagination_writes_all_rows_with_header(tmp_path: Path) -> None:
    srv = FakeServer(2500)
    rc, _, _ = run(tmp_path, srv)
    assert rc == 0
    ls = lines(tmp_path)
    assert ls[0] == bk.HEADER
    assert len(ls) == 2501
    assert srv.klines_calls() == 3           # 1000 + 1000 + 500
    first = srv.requests[2][1]               # time, exchangeInfo 다음
    assert "startTime=0" in first and "limit=1000" in first
    summ = json.loads(next((tmp_path / "meta").glob("fetch-summary-*.json")).read_text(encoding="utf-8"))
    assert summ["rows_total"] == 2500 and summ["gaps_detected"] == 0 and summ["approval"] == "A-110"
    assert summ["source_host"] == "api.binance.com" and summ["calls"] == 5
    assert not list(tmp_path.glob("*.tmp"))


def test_decimal_strings_and_iso_times_preserved(tmp_path: Path) -> None:
    run(tmp_path, FakeServer(3))
    cols = lines(tmp_path)[1].split(",")
    assert cols[1:6] == ["0.10000000", "100.5", "99.50000000", "100.123456789", "1.00000000"]
    assert cols[7:] == ["100.12345678", "7", "0.5", "50.0"]
    assert cols[0].endswith("Z") and cols[6].endswith(".999Z") and cols[0].startswith("2023-11-14T")


def test_open_bar_dropped(tmp_path: Path) -> None:
    srv = FakeServer(10, now_extra=30_000)   # 마지막 봉은 close_time > 서버 시각
    rc, _, _ = run(tmp_path, srv)
    assert rc == 0
    assert len(lines(tmp_path)) == 10        # 헤더 + 9
    summ = json.loads(next((tmp_path / "meta").glob("*.json")).read_text(encoding="utf-8"))
    assert summ["open_bar_dropped"] == 1 and summ["rows_total"] == 9


def test_gap_detection(tmp_path: Path) -> None:
    srv = FakeServer(20, skip={5, 6, 12})
    rc, _, _ = run(tmp_path, srv)
    assert rc == 0
    summ = json.loads(next((tmp_path / "meta").glob("*.json")).read_text(encoding="utf-8"))
    assert summ["gaps_detected"] == 2 and summ["gap_missing_bars"] == 3
    assert summ["first_gaps"][0]["missing_bars"] == 2


def test_existing_file_refused_without_skip_flag(tmp_path: Path) -> None:
    run(tmp_path, FakeServer(5))
    before = (tmp_path / "BTCUSDT-1m.csv").read_bytes()
    srv = FakeServer(5)
    rc, out, _ = run(tmp_path, srv)
    assert rc == 2 and srv.requests == [] and "덮어쓰지" in out[0]
    assert (tmp_path / "BTCUSDT-1m.csv").read_bytes() == before


def test_resume_appends_without_duplicates(tmp_path: Path) -> None:
    run(tmp_path, FakeServer(30))   # 앞 30봉만 가진 기존 파일
    srv = FakeServer(1500)
    rc, _, _ = run(tmp_path, srv, "--skip-existing")
    assert rc == 0
    ls = lines(tmp_path)
    assert len(ls) == 1501 and len(set(ls)) == 1501
    opens = [x.split(",")[0] for x in ls[1:]]
    assert opens == sorted(opens)
    assert f"startTime={T0 + 30 * STEP}" in srv.requests[2][1]
    summ = json.loads(sorted((tmp_path / "meta").glob("*.json"))[-1].read_text(encoding="utf-8"))
    assert summ["resumed"] is True and summ["rows_added"] == 1470 and summ["rows_total"] == 1500


def test_resume_gap_at_junction_detected(tmp_path: Path) -> None:
    run(tmp_path, FakeServer(10))
    srv = FakeServer(20, skip={10, 11})
    run(tmp_path, srv, "--skip-existing")
    summ = json.loads(sorted((tmp_path / "meta").glob("*.json"))[-1].read_text(encoding="utf-8"))
    # 서버가 startTime 이후 첫 봉(12번)을 주므로 접합부 공백(10·11번)이 잡혀야 한다
    assert summ["gaps_detected"] == 1 and summ["gap_missing_bars"] == 2


def test_resume_rejects_foreign_file_untouched(tmp_path: Path) -> None:
    f = tmp_path / "BTCUSDT-1m.csv"
    f.write_text("a,b\n1,2\n", encoding="utf-8")
    srv = FakeServer(5)
    rc, _, _ = run(tmp_path, srv, "--skip-existing")
    assert rc == 2 and f.read_text(encoding="utf-8") == "a,b\n1,2\n"


def test_429_stops_without_retry_and_saves_partial(tmp_path: Path) -> None:
    srv = FakeServer(3000)
    orig = srv.__call__
    state = {"n": 0}

    def http(m: str, u: str, h: Mapping[str, str]) -> RestReply:
        if "klines" in u:
            state["n"] += 1
            if state["n"] == 2:
                srv.requests.append((m, u, dict(h)))
                return RestReply(429, {"Retry-After": "30"}, "{}")
        return orig(m, u, h)

    out: list[str] = []
    rc = bk.klines_main(["--symbol", "BTCUSDT", "--interval", "1m", "--since", "listing", "--out", str(tmp_path)],
                        http=http, sleep=lambda s: None, mono=lambda: 0.0, out=out.append)
    assert rc == 1 and state["n"] == 2        # 재시도 없음
    assert len(lines(tmp_path)) == 1001       # 헤더 + 첫 쪽
    assert any("Retry-After=30" in o for o in out)
    summ = json.loads(next((tmp_path / "meta").glob("*.json")).read_text(encoding="utf-8"))
    assert "429" in summ["stopped"]


def test_5xx_and_timeout_retried_max_three_then_stop(tmp_path: Path) -> None:
    srv = FakeServer(5)
    srv.script = [RestReply(200, {}, json.dumps({"serverTime": srv.now}))]
    srv.script.append(RestReply(200, {}, json.dumps({"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]})))
    srv.script += [RestReply(503, {}, ""), RestAfterSendError("x"), RestReply(502, {}, "")]
    rc, out, sleeps = run(tmp_path, srv)
    assert rc == 0                               # 4번째 시도는 정상 응답
    assert 1.0 in sleeps and 2.0 in sleeps and 4.0 in sleeps
    srv2 = FakeServer(5)
    srv2.script = [RestReply(200, {}, json.dumps({"serverTime": srv2.now})),
                   RestReply(200, {}, json.dumps({"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]}))]
    srv2.script += [RestReply(500, {}, "")] * 4
    p2 = tmp_path / "b"
    rc2, out2, _ = run(p2, srv2)
    assert rc2 == 1 and srv2.klines_calls() == 4  # 1회 + 재시도 3회
    assert any("소진" in o for o in out2)


def test_other_4xx_stops_immediately(tmp_path: Path) -> None:
    srv = FakeServer(5)
    srv.script = [RestReply(200, {}, json.dumps({"serverTime": srv.now})),
                  RestReply(200, {}, json.dumps({"symbols": [{"symbol": "BTCUSDT", "status": "TRADING"}]})),
                  RestReply(400, {}, "{}")]
    rc, _, _ = run(tmp_path, srv)
    assert rc == 1 and srv.klines_calls() == 1


def test_min_spacing_between_calls(tmp_path: Path) -> None:
    srv = FakeServer(2500)
    _, _, sleeps = run(tmp_path, srv)
    assert len(sleeps) >= 4 and all(s >= 0.25 - 1e-9 for s in sleeps)


def test_until_limits_range_and_sends_end_time(tmp_path: Path) -> None:
    srv = FakeServer(10)
    day = "2023-11-14"
    rc, _, _ = run(tmp_path, srv, "--until", day, since=day, interval="1m")
    assert rc == 0
    u = srv.requests[2][1]
    assert "endTime=" in u and "startTime=" in u


def test_since_date_used_as_start_time(tmp_path: Path) -> None:
    srv = FakeServer(3)
    run(tmp_path, srv, since="2023-11-14")
    assert "startTime=1699920000000" in srv.requests[2][1]


@pytest.mark.parametrize("args", [
    ["--symbol", "btc", "--interval", "1m", "--since", "listing"],
    ["--symbol", "BTCUSDT", "--interval", "5m", "--since", "listing"],
    ["--symbol", "BTCUSDT", "--interval", "1m", "--since", "2023-13-01"],
    ["--symbol", "BTCUSDT", "--interval", "1m", "--since", "2023-02-01", "--until", "2023-01-01"],
])
def test_bad_args_refused_without_network(tmp_path: Path, args: list[str]) -> None:
    srv = FakeServer(3)
    rc = bk.klines_main([*args, "--out", str(tmp_path)], http=srv, sleep=lambda s: None, out=lambda s: None)
    assert rc == 2 and srv.requests == []


# ---- 가드 ----

def _guard() -> tuple[bk.PublicGuard, list[str]]:
    sent: list[str] = []

    def inner(m: str, u: str, h: Mapping[str, str]) -> RestReply:
        sent.append(u)
        return RestReply(200, {}, "[]")

    return bk.PublicGuard(inner), sent


@pytest.mark.parametrize("method,url", [
    ("GET", "https://testnet.binance.vision/api/v3/klines?symbol=BTCUSDT"),
    ("GET", "https://api1.binance.com/api/v3/klines?symbol=BTCUSDT"),
    ("GET", "https://api.binance.com.evil.com/api/v3/time"),
    ("GET", "http://api.binance.com/api/v3/time"),
    ("GET", "https://api.binance.com:8443/api/v3/time"),
    ("GET", "https://user@api.binance.com/api/v3/time"),
    ("GET", "https://api.binance.com/api/v3/order?symbol=BTCUSDT"),
    ("GET", "https://api.binance.com/api/v3/account"),
    ("GET", "https://api.binance.com/api/v3/openOrders"),
    ("GET", "https://api.binance.com/sapi/v1/capital/config/getall"),
    ("GET", "https://api.binance.com/api/v3/userDataStream"),
    ("GET", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&signature=ab"),
    ("GET", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&timestamp=1"),
    ("GET", "https://api.binance.com/api/v3/time?x=1"),
    ("POST", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT"),
    ("DELETE", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT"),
    ("PUT", "https://api.binance.com/api/v3/time"),
])
def test_guard_refuses_before_send(method: str, url: str) -> None:
    g, sent = _guard()
    with pytest.raises(bk.GuardError):
        g(method, url, {})
    assert sent == [] and g.sent == 0


@pytest.mark.parametrize("hdr", [{"X-MBX-APIKEY": "k"}, {"x-mbx-apikey": "k"}, {"Authorization": "x"}, {"User-Agent": "a"}])
def test_guard_refuses_any_headers(hdr: dict[str, str]) -> None:
    g, sent = _guard()
    with pytest.raises(bk.GuardError):
        g("GET", "https://api.binance.com/api/v3/time", hdr)
    assert sent == []


def test_guard_allows_three_public_endpoints_with_empty_headers() -> None:
    g, sent = _guard()
    g("GET", "https://api.binance.com/api/v3/time", {})
    g("GET", "https://api.binance.com/api/v3/exchangeInfo?symbol=BTCUSDT", {})
    g("GET", "https://api.binance.com/api/v3/klines?symbol=BTCUSDT&interval=1m&startTime=0&limit=1000", {})
    assert len(sent) == 3


def test_main_sends_no_headers_and_only_allowed_paths(tmp_path: Path) -> None:
    srv = FakeServer(1500)
    run(tmp_path, srv)
    assert all(h == {} and m == "GET" for m, _, h in srv.requests)
    assert {urlsplit(u).path for _, u, _ in srv.requests} <= set(bk.ALLOWED_PATHS)
    assert {urlsplit(u).hostname for _, u, _ in srv.requests} == {"api.binance.com"}


def test_default_transport_limited_to_public_host() -> None:
    assert bk.ALLOWED_HOSTS == frozenset({"api.binance.com"})
    assert "testnet" not in bk.HOST


def test_daily_interval_step(tmp_path: Path) -> None:
    srv = FakeServer(5, step=86_400_000)
    # 서버 봉은 일봉 간격, 시작은 임의 정렬 — 간격 일치로 공백 0
    srv.now = srv.bars[-1][6] + 1  # type: ignore[operator]
    rc, _, _ = run(tmp_path, srv, interval="1d")
    assert rc == 0
    summ = json.loads(next((tmp_path / "meta").glob("*.json")).read_text(encoding="utf-8"))
    assert summ["interval"] == "1d" and summ["gaps_detected"] == 0 and summ["rows_total"] == 5
    assert (tmp_path / "BTCUSDT-1d.csv").exists()
