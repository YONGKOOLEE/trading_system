"""A-107 종목명 → 종목코드 변환 도구 — 네트워크 없이 가짜 ka10099 응답으로만 시험한다.

- `--mode PAPER`만, 실행 잠금(run.lock)·하트비트 있으면 연결 전에 거부, 허용 API는 au10001·au10002·ka10099뿐.
- exact / normalized / 없음 / 동명(후보 표시) / 우선주·ETF만 있는 이름 / 중복 입력 / 코스닥 포함.
- 출력 형식(codes.txt·name-map.csv·unmatched.csv), 덮어쓰기 거부, 비밀 없음, 토큰 폐기.
"""

from __future__ import annotations

import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from trading_system.app import name_resolve
from trading_system.app.daily_fetch import read_codes
from trading_system.app.instance_lock import acquire_instance_lock
from trading_system.app.name_resolve import normalize, resolve_main
from trading_system.broker.base import BrokerQueryError
from trading_system.app.preflight import GuardedHttp
from trading_system.broker.kiwoom.http import PreSendError
from trading_system.domain.clock import FixedClock

T0 = datetime(2026, 10, 7, 12, 0, tzinfo=UTC)
SECRET = "SECRET-APPKEY-0123456789"


def row(code: str, name: str, market: str = "거래소") -> dict:
    return {"code": code, "name": name, "state": "증거금20%", "auditInfo": "정상", "orderWarning": "0",
            "marketName": market}


MASTER = {
    "0": [row("005930", "삼성전자"), row("005935", "삼성전자우"), row("000660", "SK하이닉스"),
          row("010950", "S-Oil"), row("003550", "LG"), row("123450", "동명A"), row("123460", "동명A"),
          row("069500", "KODEX 200"), row("005380", "현대차")],
    "10": [row("247540", "에코프로비엠", "코스닥"), row("035720", "카카오"), row("000880", "한화"),
           row("018260", "ＡＢＣ 테크")],
    "8": [row("069500", "KODEX 200"), row("999990", "삼성전자")],      # ETF 이름이 보통주와 같은 경우(후보 표시용)
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

    def query_raw(self, path: str, api_id: str, body: dict, list_key: str | None, enough=None):  # type: ignore[no-untyped-def]
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


def run(tmp_path: Path, argv: list[str], fake: FakeAdapter | None = None, *, clock: FixedClock | None = None) -> int:
    fake = fake or FakeAdapter()

    def connect(root, environ, clk):  # type: ignore[no-untyped-def]
        return FakeConn(fake)
    return resolve_main(tmp_path, argv, {"TRADE_MODE": "PAPER"}, clock=clock or FixedClock(T0), connect=connect)


def names_file(tmp_path: Path, text: str) -> Path:
    p = tmp_path / "names.txt"
    p.write_text(text, encoding="utf-8")
    return p


def args(tmp_path: Path, *extra: str) -> list[str]:
    return ["--mode", "PAPER", "--names", str(tmp_path / "names.txt"), "--out", str(tmp_path / "out"), *extra]


def table(path: Path) -> list[dict]:
    with path.open(encoding="utf-8", newline="") as f:
        return list(csv.DictReader(f))


# --- 순수 함수 -------------------------------------------------------------------------------------

def test_normalize_unifies_width_space_case() -> None:
    assert normalize("ＡＢＣ 테크") == normalize("abc테크") == "abc테크"
    assert normalize(" S - Oil") == "s-oil"
    assert normalize("   ") == ""


# --- 정상 경로 -------------------------------------------------------------------------------------

def test_exact_normalized_kosdaq_and_outputs(tmp_path: Path) -> None:
    names_file(tmp_path, "# 주석\n삼성전자\n  SK하이닉스  # 뒤 주석\n에코프로비엠\n카카오\ns-oil\nabc테크\n\n")
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path), fake) == 0
    out = tmp_path / "out"
    rows = table(out / "name-map.csv")
    assert list(rows[0].keys()) == ["name", "code", "market", "match"]
    got = {r["name"]: (r["code"], r["market"], r["match"]) for r in rows}
    assert got["삼성전자"] == ("005930", "KOSPI", "exact")        # 같은 이름 ETF(999990)가 있어도 보통주 하나면 선택
    assert got["SK하이닉스"] == ("000660", "KOSPI", "exact")
    assert got["에코프로비엠"] == ("247540", "KOSDAQ", "exact")   # 코스닥 포함
    assert got["카카오"] == ("035720", "KOSDAQ", "exact")
    assert got["s-oil"] == ("010950", "KOSPI", "normalized")
    assert got["abc테크"] == ("018260", "KOSDAQ", "normalized")
    assert (out / "codes.txt").read_text(encoding="utf-8").split() == \
        ["005930", "000660", "247540", "035720", "010950", "018260"]
    assert table(out / "unmatched.csv") == []
    assert (out / "unmatched.csv").read_text(encoding="utf-8").splitlines() == ["name,reason"]
    assert [a for a, _ in fake.calls] == ["ka10099"] * 4
    assert sorted(b["mrkt_tp"] for _, b in fake.calls) == ["0", "10", "60", "8"]
    assert fake.closed and fake.order_calls == []


def test_codes_txt_is_accepted_by_fetch_daily(tmp_path: Path) -> None:
    names_file(tmp_path, "삼성전자\n카카오\n")
    assert run(tmp_path, args(tmp_path)) == 0
    assert read_codes(tmp_path / "out" / "codes.txt") == ["005930", "035720"]


def test_not_found_ambiguous_preferred_only_and_no_fuzzy(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    names_file(tmp_path, "없는회사\n동명A\nKODEX 200\n삼성전\nETN 예시\n현대차\n")
    assert run(tmp_path, args(tmp_path)) == 0
    out = tmp_path / "out"
    un = {r["name"]: r["reason"] for r in table(out / "unmatched.csv")}
    assert "없음" in un["없는회사"] and "상장폐지" in un["없는회사"]
    assert "동명 여러 개" in un["동명A"] and "123450" in un["동명A"] and "123460" in un["동명A"]
    assert "보통주 아님" in un["KODEX 200"] and "069500" in un["KODEX 200"]       # ETF만 있는 이름
    assert "없음" in un["삼성전"]                                                    # 부분 문자열은 매칭하지 않음
    assert "보통주 아님" in un["ETN 예시"] and "580001" in un["ETN 예시"]
    assert [r["name"] for r in table(out / "name-map.csv")] == ["현대차"]
    assert (out / "codes.txt").read_text(encoding="utf-8") == "005380\n"
    assert "매칭 1" in capsys.readouterr().out


def test_exact_beats_normalized_and_preferred_name_is_distinct(tmp_path: Path) -> None:
    names_file(tmp_path, "삼성전자우\n삼성 전자\n")
    assert run(tmp_path, args(tmp_path)) == 0
    un = {r["name"]: r["reason"] for r in table(tmp_path / "out" / "unmatched.csv")}
    assert "보통주 아님" in un["삼성전자우"] and "005935" in un["삼성전자우"]        # 우선주는 골라 주지 않음
    rows = {r["name"]: r for r in table(tmp_path / "out" / "name-map.csv")}
    # '삼성 전자' 정규화 → 삼성전자 (보통주 005930 하나, ETF 999990은 비보통주)
    assert rows["삼성 전자"]["code"] == "005930" and rows["삼성 전자"]["match"] == "normalized"


def test_duplicate_names_resolved_once_and_codes_deduped(tmp_path: Path) -> None:
    names_file(tmp_path, "카카오\n 카카오 \n카 카오\n카카오\n")
    assert run(tmp_path, args(tmp_path)) == 0
    out = tmp_path / "out"
    rows = table(out / "name-map.csv")
    assert [(r["name"], r["match"]) for r in rows] == [("카카오", "exact"), ("카 카오", "normalized")]
    assert (out / "codes.txt").read_text(encoding="utf-8") == "035720\n"


# --- dry-run · 거부 ---------------------------------------------------------------------------------

def test_dry_run_validates_only(tmp_path: Path) -> None:
    names_file(tmp_path, "삼성전자\n")
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path, "--dry-run"), fake) == 0
    assert fake.calls == [] and not fake.closed and not (tmp_path / "out").exists()


@pytest.mark.parametrize("text", ["", "# 주석만\n\n   \n"])
def test_empty_names_file_refused(tmp_path: Path, text: str) -> None:
    names_file(tmp_path, text)
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path, "--dry-run"), fake) == 2
    assert run(tmp_path, args(tmp_path), fake) == 2
    assert fake.calls == []


def test_missing_names_file_refused(tmp_path: Path) -> None:
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path), fake) == 2 and fake.calls == []


@pytest.mark.parametrize("argv", [["--mode", "LIVE"], ["--mode", "BACKTEST"], ["--mode", "paper"], []])
def test_refuses_non_paper_mode(tmp_path: Path, argv: list[str]) -> None:
    names_file(tmp_path, "삼성전자\n")
    fake = FakeAdapter()
    extra = ["--names", str(tmp_path / "names.txt"), "--out", str(tmp_path / "out")] if argv else []
    assert run(tmp_path, argv + extra, fake) == 2
    assert fake.calls == [] and not fake.closed


def test_refuses_while_run_lock_held(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    names_file(tmp_path, "삼성전자\n")
    lock = acquire_instance_lock(tmp_path / "var" / "paper")
    try:
        fake = FakeAdapter()
        assert run(tmp_path, args(tmp_path), fake) == 2
    finally:
        lock.release()
    assert fake.calls == [] and not (tmp_path / "out").exists()
    assert "실행 중" in capsys.readouterr().out


def test_refuses_while_heartbeat_fresh_and_old_one_passes(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    names_file(tmp_path, "삼성전자\n")
    hb = tmp_path / "var" / "paper" / "heartbeat"
    hb.parent.mkdir(parents=True)
    hb.write_text((T0 - timedelta(seconds=10)).isoformat(), encoding="utf-8")
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path), fake) == 2 and fake.calls == []
    assert "하트비트" in capsys.readouterr().out
    hb.write_text((T0 - timedelta(seconds=120)).isoformat(), encoding="utf-8")
    assert run(tmp_path, args(tmp_path), fake) == 0


def test_refuses_when_connection_refused(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    names_file(tmp_path, "삼성전자\n")
    code = resolve_main(tmp_path, args(tmp_path), {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0),
                        connect=lambda root, env, clk: "connection_approved = true가 아니다")
    assert code == 2 and "connection_approved" in capsys.readouterr().out


def test_refuses_to_overwrite_existing_output(tmp_path: Path) -> None:
    names_file(tmp_path, "삼성전자\n")
    out = tmp_path / "out"
    out.mkdir()
    (out / "codes.txt").write_text("keep\n", encoding="utf-8")
    fake = FakeAdapter()
    assert run(tmp_path, args(tmp_path), fake) == 2 and fake.calls == []
    assert (out / "codes.txt").read_text(encoding="utf-8") == "keep\n"


# --- 실패·허용 API 가드 -----------------------------------------------------------------------------

@pytest.mark.parametrize("bad", ["0", "10", "8", "60"])
def test_query_failure_writes_nothing_and_revokes_token(tmp_path: Path, bad: str) -> None:
    names_file(tmp_path, "삼성전자\n")
    fake = FakeAdapter(fail={bad})
    assert run(tmp_path, args(tmp_path), fake) == 1
    assert fake.closed and not (tmp_path / "out").exists()


def test_empty_stock_market_list_is_not_judged(tmp_path: Path) -> None:
    names_file(tmp_path, "삼성전자\n")
    fake = FakeAdapter({**MASTER, "10": []})
    assert run(tmp_path, args(tmp_path), fake) == 1
    assert fake.closed and not (tmp_path / "out").exists()


def test_default_connect_uses_resolve_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    names_file(tmp_path, "삼성전자\n")
    seen: dict = {}

    def fake_connect(root, environ, clock, **kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return "거부(시험)"
    monkeypatch.setattr(name_resolve, "connect_paper", fake_connect)
    assert resolve_main(tmp_path, args(tmp_path), {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0)) == 2
    assert seen["allowed_apis"] == frozenset({"au10001", "au10002", "ka10099"})
    assert not (seen["allowed_apis"] & name_resolve.ORDER_API_IDS)


@pytest.mark.parametrize("api_id", ["ka10001", "ka10081", "ka20002", "ka20006", "kt10000", "kt10001", "kt10002",
                                    "kt10003", "ka10075"])
def test_guarded_http_blocks_every_other_api(api_id: str) -> None:
    sent: list[str] = []

    class Inner:
        calls = 0

        def post(self, path, api, body, token, cont_yn=None, next_key=None):  # type: ignore[no-untyped-def]
            sent.append(api)
            raise AssertionError("전송되면 안 됨")
    guard = GuardedHttp(Inner(), name_resolve.RESOLVE_APIS)  # type: ignore[arg-type]
    with pytest.raises(PreSendError):
        guard.post("/api/dostk/x", api_id, {}, "tok")
    assert sent == [] and guard.blocked == [api_id]


def test_blocked_order_api_attempt_discards_results(tmp_path: Path) -> None:
    names_file(tmp_path, "삼성전자\n")
    fake = FakeAdapter()
    conn = FakeConn(fake)
    conn.guard.blocked.append("kt10000")
    code = resolve_main(tmp_path, args(tmp_path), {"TRADE_MODE": "PAPER"}, clock=FixedClock(T0),
                        connect=lambda r, e, c: conn)
    assert code == 1 and fake.closed and not (tmp_path / "out").exists()


# --- 비밀 ------------------------------------------------------------------------------------------

def test_outputs_and_console_contain_no_secrets(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    names_file(tmp_path, "삼성전자\n없는회사\n")
    master = {**MASTER, "0": [{**r, "appkey": SECRET} for r in MASTER["0"]]}      # 응답에 낯선 필드가 있어도 복사하지 않음
    assert run(tmp_path, args(tmp_path), FakeAdapter(master)) == 0
    text = capsys.readouterr().out
    for f in (tmp_path / "out").iterdir():
        text += f.read_text(encoding="utf-8")
    assert SECRET not in text
    assert sorted(p.name for p in (tmp_path / "out").iterdir()) == ["codes.txt", "name-map.csv", "unmatched.csv"]
