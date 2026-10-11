"""전략실 연구용 데이터 확장 (A-087) — 코스피200 구성 종목(ka20002) + KOSPI 지수 일봉(ka20006). 네트워크 없음.

- `--kospi200`: ka20002(mrkt_tp 2, inds_cd 201, stex_tp 1) 현재 구성 종목 → 종목별 ka10081 수정주가 일봉.
  구성 목록은 meta/kospi200-constituents.csv(생존편향 표시). 코드 형식이 아니면 추측하지 않고 중단.
- `--index 001`: ka20006(inds_cd, base_dt) 일봉 → `<out>/index/<코드>.csv`. 값은 100배 정수 → 소수 둘째 자리 지수.
- 기본 연결은 허용 API 목록(au10001·au10002·ka10099·ka10001·ka10081·ka20002·ka20006)으로 그 밖의 API를 전송 전 차단.
"""

from __future__ import annotations

import json
from datetime import date, timedelta
from pathlib import Path

import pytest

from tests.scenario.test_t018_daily_fetch import T0, UNTIL, FakeAdapter, daily_rows, make_fake, run
from trading_system.app import daily_fetch
from trading_system.app.main import load_daily_csv
from trading_system.broker.base import BrokerQueryError
from trading_system.domain.clock import FixedClock


def index_rows(first: date, last: date, base: int) -> list[dict]:
    out, d, i = [], last, 0
    while d >= first:
        if d.weekday() < 5:
            p = base + i
            out.append({"dt": d.strftime("%Y%m%d"), "open_pric": str(p - 50), "high_pric": str(p + 120),
                        "low_pric": str(p - 130), "cur_prc": str(p), "trde_qty": str(400000 + i),
                        "trde_prica": "10582466"})
            i += 1
        d -= timedelta(days=1)
    return out   # 최신 → 과거


class SectFake(FakeAdapter):
    def __init__(self, constituents: list[dict], index: dict[str, list[dict]] | None = None,
                 fail_index: bool = False, **kw) -> None:  # type: ignore[no-untyped-def]
        base = make_fake()
        daily = dict(base.daily)
        for c in constituents:
            daily.setdefault(str(c.get("stk_cd")), daily_rows(date(2015, 11, 2), UNTIL, 30000))
        super().__init__(base.kospi, base.caps, daily, etf=base.etf, **kw)
        self.constituents, self.index, self.fail_index = constituents, index or {}, fail_index
        self.paths: list[tuple[str, str]] = []

    def query_raw(self, path: str, api_id: str, body: dict, list_key: str | None, enough=None):  # type: ignore[no-untyped-def]
        self.paths.append((api_id, path))
        if api_id == "ka20002":
            self.calls.append((api_id, dict(body)))
            assert list_key == "inds_stkpc"
            return {}, list(self.constituents)
        if api_id == "ka20006":
            self.calls.append((api_id, dict(body)))
            assert list_key == "inds_dt_pole_qry"
            if self.fail_index:
                raise BrokerQueryError("ka20006: 코드 1999")
            rows: list[dict] = []
            for r in self.index.get(body["inds_cd"], []):
                rows.append(r)
                if enough is not None and len(rows) % 100 == 0 and enough(rows):
                    break
            return {"inds_cd": body["inds_cd"]}, rows
        return super().query_raw(path, api_id, body, list_key, enough)


K200 = [{"stk_cd": "005930", "stk_nm": "삼성전자", "cur_prc": "+270000"},
        {"stk_cd": "000660", "stk_nm": "SK하이닉스", "cur_prc": "-1236000"},
        {"stk_cd": "005380", "stk_nm": "현대차", "cur_prc": "244000"}]
INDEX = {"001": index_rows(date(2015, 11, 2), UNTIL, 252127)}


def test_kospi200_constituents_drive_daily_fetch(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = SectFake(K200, INDEX)
    out = tmp_path / "data"
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--out", str(out)], fake) == 0
    text = capsys.readouterr().out
    assert fake.order_calls == [] and fake.closed
    assert fake.count("ka10099") == 0 and fake.count("ka10001") == 0          # 시가총액 순위 생략
    (api, body), = [(a, b) for a, b in fake.calls if a == "ka20002"]
    assert body == {"mrkt_tp": "2", "inds_cd": "201", "stex_tp": "1"}
    assert ("ka20002", "/api/dostk/sect") in fake.paths
    assert sorted(p.name for p in out.glob("*.csv")) == ["000660.csv", "005380.csv", "005930.csv"]
    assert load_daily_csv(out / "005930.csv")[0].day == date(2016, 1, 1)
    lines = (out / "meta" / "kospi200-constituents.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "code,name,basis"
    assert [ln.split(",")[0] for ln in lines[1:]] == ["005930", "000660", "005380"]
    assert "생존편향" in lines[1]
    assert "구성 종목 3" in text and "200이 아님" in text                       # 개수 경고(중단 아님)
    summary = json.loads(next((out / "meta").glob("fetch-summary-*.json")).read_text(encoding="utf-8"))
    assert summary["universe"].startswith("ka20002") and summary["ok"] == 3


def test_kospi200_bad_code_stops_without_guessing(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = SectFake(K200 + [{"stk_cd": "A005935", "stk_nm": "이상코드"}])
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--out", str(tmp_path / "d")], fake) == 1
    assert fake.count("ka10081") == 0 and fake.closed
    assert "종목코드 형식 아님" in capsys.readouterr().out


def test_kospi200_empty_list_stops(tmp_path: Path) -> None:
    fake = SectFake([])
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--out", str(tmp_path / "d")], fake) == 1
    assert fake.count("ka10081") == 0


def test_kospi200_duplicates_kept_once(tmp_path: Path) -> None:
    fake = SectFake(K200 + [K200[0]])
    out = tmp_path / "d"
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--out", str(out)], fake) == 0
    assert fake.count("ka10081") == 3


def test_kospi200_dry_run_lists_only(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = SectFake(K200)
    out = tmp_path / "d"
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--dry-run", "--out", str(out)], fake) == 0
    assert fake.count("ka10081") == 0 and fake.count("ka20006") == 0
    assert (out / "meta" / "kospi200-constituents.csv").exists()


def test_kospi200_and_codes_are_exclusive(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    codes = tmp_path / "c.txt"
    codes.write_text("005930\n", encoding="utf-8")
    fake = SectFake(K200)
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--codes", str(codes)], fake) == 2
    assert fake.calls == []


def test_index_daily_scaled_and_separate_folder(tmp_path: Path) -> None:
    fake = SectFake(K200, INDEX)
    out = tmp_path / "data"
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--index", "001", "--out", str(out)], fake) == 0
    (_, body), = [(a, b) for a, b in fake.calls if a == "ka20006"]
    assert body == {"inds_cd": "001", "base_dt": "20261007"}
    assert not (out / "001.csv").exists()                                   # 종목 CSV와 섞이지 않음
    lines = (out / "index" / "001.csv").read_text(encoding="utf-8").splitlines()
    assert lines[0] == "date,open,high,low,close,volume"
    last = lines[-1].split(",")
    assert last[0] == "2026-10-07" and last[4] == "2521.27" and last[1] == "2520.77"
    assert lines[1].startswith("2016-01-01,")
    dates = [ln.split(",")[0] for ln in lines[1:]]
    assert dates == sorted(dates) and len(set(dates)) == len(dates)
    bars = load_daily_csv(out / "index" / "001.csv")                       # 같은 로더로 읽힌다
    assert str(bars[-1].close) == "2521.27"
    summary = json.loads(next((out / "meta").glob("fetch-summary-*.json")).read_text(encoding="utf-8"))
    assert summary["index"]["001"]["rows"] == len(lines) - 1


def test_index_failure_recorded_stocks_kept(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    fake = SectFake(K200, fail_index=True)
    out = tmp_path / "data"
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--index", "001", "--out", str(out)], fake) == 1
    assert len(list(out.glob("*.csv"))) == 3
    assert "지수 001 실패" in capsys.readouterr().out


@pytest.mark.parametrize("value", ["1", "999", "KOSPI", "0010"])
def test_index_code_must_be_known(tmp_path: Path, value: str) -> None:
    fake = SectFake(K200, INDEX)
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--index", value], fake) == 2
    assert fake.calls == []


def test_index_existing_file_refused(tmp_path: Path) -> None:
    out = tmp_path / "data"
    (out / "index").mkdir(parents=True)
    (out / "index" / "001.csv").write_text("date\n", encoding="utf-8")
    fake = SectFake(K200, INDEX)
    assert run(tmp_path, ["--mode", "PAPER", "--codes", str(_codes(tmp_path)), "--index", "001",
                          "--out", str(out)], fake) == 2
    assert fake.calls == []


def _codes(tmp_path: Path) -> Path:
    p = tmp_path / "codes.txt"
    p.write_text("005930\n", encoding="utf-8")
    return p


def test_default_out_folder_named_for_kospi200(tmp_path: Path) -> None:
    fake = SectFake(K200, INDEX)
    assert run(tmp_path, ["--mode", "PAPER", "--kospi200", "--index", "001"], fake) == 0
    out = tmp_path / "var" / "backtest" / "data" / "kiwoom-kospi200-adj-20261007"
    assert len(list(out.glob("*.csv"))) == 3 and (out / "index" / "001.csv").exists()


def test_default_connect_uses_fetch_allowlist(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    seen: dict = {}

    def fake_connect(root, environ, clock, **kw):  # type: ignore[no-untyped-def]
        seen.update(kw)
        return "거부(시험)"
    monkeypatch.setattr(daily_fetch, "connect_paper", fake_connect)
    assert daily_fetch.fetch_main(tmp_path, ["--mode", "PAPER", "--kospi200"], {"TRADE_MODE": "PAPER"},
                                  clock=FixedClock(T0)) == 2
    assert seen["allowed_apis"] == frozenset({"au10001", "au10002", "ka10099", "ka10001", "ka10081",
                                              "ka20002", "ka20006"})
    assert not (seen["allowed_apis"] & daily_fetch.ORDER_API_IDS)
