"""연구용 투자자 데이터 수집 모드 `--collect` (A-090) — 합성 응답만, 네트워크 없음.

- 종목 목록 파일(kospi200-constituents.csv 형식 `code,name,basis` 또는 한 줄에 하나)의 종목마다 ka10060 금액·순매수를
  마지막 거래일부터 연속 조회하되, **since보다 오래된 일자가 나온 쪽에서 멈춘다**(그 이전 쪽은 받지 않음).
- `<out>/<코드>.csv` 열 = dt, acc_trde_prica, 투자자 구분 13개(응답 문자열 그대로, 일자 오름차순, since 이후만).
- meta-<시각>.json: 호출 수·범위·종목별 결과·실패 종목·미조회 종목. 허용 API au10001·au10002·ka10060.
"""

from __future__ import annotations

import json
from datetime import UTC, date, datetime
from pathlib import Path
from typing import Any

import pytest

from tests.scenario.test_t034_investor_probe import (ACCOUNT_NO, APP_KEY, PAGE, PRICE, SECRET, TOKEN, FakeInner,
                                                     Harness, kst, read_csv, weekdays_desc)
from trading_system.app import investor_probe
from trading_system.app.instance_lock import acquire_instance_lock
from trading_system.app.investor_probe import COLLECT_COLUMNS, DEEP_REST_APIS
from trading_system.broker.kiwoom.http import HttpReply
from trading_system.domain.clock import FixedClock

SINCE = date(2025, 1, 1)
LAST = date(2026, 10, 8)
OUT = Path("var") / "backtest" / "data" / "kiwoom-investor-20261008"


def harness(tmp_path: Path, **kw: Any) -> Harness:
    return Harness(tmp_path, allow=DEEP_REST_APIS, **kw)


def codes_file(h: Harness, codes: list[str], header: bool = True) -> Path:
    p = h.root / "list.csv"
    lines = (["code,name,basis"] if header else []) + [f"{c},이름{c},ka20002 현재 구성" for c in codes]
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def args(path: Path, *extra: str) -> list[str]:
    return ["--mode", "PAPER", "--collect", "--codes-file", str(path), "--since", SINCE.isoformat(), *extra]


def meta_of(h: Harness, out: Path = OUT) -> dict:
    metas = sorted((h.root / out).glob("meta-*.json"))
    assert len(metas) == 1
    return json.loads(metas[0].read_text(encoding="utf-8"))


def test_collect_stops_at_since_and_saves_per_code(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = harness(tmp_path)
    assert h.run(args(codes_file(h, ["005930", "000660", "0126Z0"]))) == 0
    out = capsys.readouterr().out
    assert h.inner.apis() == DEEP_REST_APIS and h.inner.log[-1][1] == "au10002"
    n_since = len([d for d in weekdays_desc(LAST) if d >= SINCE])
    pages_needed = n_since // PAGE + 1                              # since보다 오래된 행이 처음 나오는 쪽까지만
    per_code = [b for _, a, b, _ in h.inner.log if a == "ka10060"]
    assert len(per_code) == 3 * pages_needed                        # 그 이전 쪽은 받지 않음
    assert all(b["dt"] == "20261008" and b["amt_qty_tp"] == "1" and b["trde_tp"] == "0" for b in per_code)
    assert [b["stk_cd"] for b in per_code[::pages_needed]] == ["005930", "000660", "0126Z0"]
    for code in ("005930", "000660", "0126Z0"):
        cols, rows = read_csv(h.root / OUT / f"{code}.csv")
        assert cols == list(COLLECT_COLUMNS)
        assert cols[:2] == ["dt", "acc_trde_prica"] and "cur_prc" not in cols and "pred_pre" not in cols
        assert min(r["dt"] for r in rows) >= "20250101" and rows[-1]["dt"] == "20261008"
        assert len(rows) == n_since and [r["dt"] for r in rows] == sorted(r["dt"] for r in rows)
        assert rows[-1]["ind_invsr"].startswith("+")                 # 응답 문자열 그대로
    meta = meta_of(h)
    assert meta["since"] == "20250101" and meta["until"] == "20261008"
    assert meta["codes_requested"] == 3 and meta["ok"] == 3 and meta["failed"] == [] and meta["not_queried"] == []
    assert meta["calls"] == 1 + 3 * pages_needed + 1
    assert meta["per_code"]["005930"]["rows"] == n_since and meta["per_code"]["005930"]["pages"] == pages_needed
    assert meta["columns"] == list(COLLECT_COLUMNS) and meta["interval_seconds"] >= 1.2
    text = out + json.dumps(meta, ensure_ascii=False)
    for secret in (TOKEN, APP_KEY, SECRET, ACCOUNT_NO, PRICE):
        assert secret not in text
    assert "종목 3" in out and "실패 0" in out


def test_collect_default_out_folder_and_plain_list(tmp_path: Path) -> None:
    h = harness(tmp_path)
    assert h.run(args(codes_file(h, ["005930"], header=False))) == 0
    assert (h.root / OUT / "005930.csv").exists()


def test_collect_failed_code_recorded_others_saved(tmp_path: Path) -> None:
    calls = {"n": 0}

    class Flaky(FakeInner):
        def post(self, path, api_id, body, token, cont_yn=None, next_key=None):  # type: ignore[no-untyped-def]
            if api_id == "ka10060" and body.get("stk_cd") == "000660":
                calls["n"] += 1
                return HttpReply(200, {"return_code": 5, "return_msg": "기타 업무 오류"})
            return super().post(path, api_id, body, token, cont_yn, next_key)

    h = harness(tmp_path, inner=Flaky())
    assert h.run(args(codes_file(h, ["005930", "000660", "035420"]))) == 1
    meta = meta_of(h)
    assert [f["code"] for f in meta["failed"]] == ["000660"] and meta["ok"] == 2
    assert not (h.root / OUT / "000660.csv").exists() and (h.root / OUT / "035420.csv").exists()
    assert calls["n"] == 3                                          # 같은 쪽 3회 연속 실패면 그 종목 중단


def test_collect_three_failed_codes_in_a_row_stop_rest(tmp_path: Path) -> None:
    h = harness(tmp_path, inner=FakeInner(fail={"ka10060": {"return_code": 5, "return_msg": "기타 업무 오류"}}))
    assert h.run(args(codes_file(h, ["005930", "000660", "035420", "005380", "373220"]))) == 1
    meta = meta_of(h)
    assert len(meta["failed"]) == 3 and meta["not_queried"] == ["005380", "373220"]
    assert h.inner.log[-1][1] == "au10002"


def test_collect_refuses_existing_csv_without_connecting(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    h = harness(tmp_path)
    (h.root / OUT).mkdir(parents=True)
    (h.root / OUT / "000660.csv").write_text("dt\n", encoding="utf-8")
    assert h.run(args(codes_file(h, ["005930", "000660"]))) == 2
    assert h.connects == 0 and "덮어쓰지 않는다" in capsys.readouterr().out


@pytest.mark.parametrize("bad", [
    "code,name\n005930_AL,x\n",       # 코드 형식 아님(거래소 접미사 — 6자리 영숫자 신형 코드는 유효)
    "code,name,basis\n",              # 종목 없음
])
def test_collect_refuses_bad_codes_file(tmp_path: Path, bad: str) -> None:
    h = harness(tmp_path)
    p = h.root / "list.csv"
    p.write_text(bad, encoding="utf-8")
    assert h.run(args(p)) == 2 and h.connects == 0


@pytest.mark.parametrize("extra", [["--since", "2016-13-01"], ["--deep"], ["--codes", "005930"]])
def test_collect_refuses_bad_or_conflicting_args(tmp_path: Path, extra: list[str]) -> None:
    h = harness(tmp_path)
    assert h.run(args(codes_file(h, ["005930"]), *extra)) == 2 and h.connects == 0


def test_collect_requires_codes_file(tmp_path: Path) -> None:
    h = harness(tmp_path)
    assert h.run(["--mode", "PAPER", "--collect"]) == 2 and h.connects == 0


def test_collect_refuses_market_hours_and_run_lock(tmp_path: Path) -> None:
    h = harness(tmp_path, when=kst(2026, 10, 8, 10, 0).astimezone(UTC))
    assert h.run(args(codes_file(h, ["005930"]))) == 2 and h.connects == 0
    h2 = harness(tmp_path / "b")
    lock = acquire_instance_lock(h2.root / "var" / "paper", "autopilot.lock")
    try:
        assert h2.run(args(codes_file(h2, ["005930"]))) == 2
    finally:
        lock.release()
    assert h2.connects == 0


def test_collect_uses_deep_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: dict = {}
    monkeypatch.setattr(investor_probe, "connect_paper", lambda root, env, clock, **kw: seen.update(kw) or "거부")
    h = harness(tmp_path)
    assert investor_probe.investor_main(h.root, args(codes_file(h, ["005930"])), {"TRADE_MODE": "PAPER"},
                                        clock=FixedClock(datetime(2026, 10, 8, 11, 0, tzinfo=UTC))) == 2
    assert seen["allowed_apis"] == DEEP_REST_APIS and seen["per_tr_interval"] >= 1.2
