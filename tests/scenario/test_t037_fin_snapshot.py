"""T-037 CAP-DAT-05b 재무 스냅샷 축적 (CP-033 D-194, 승인 A-088 Q-4 (b)·A-100 Q-6 (a)).

합성 응답만(네트워크 없음). 모든 값은 가상이다.
- V-265: ka10001 per·eps·roe·pbr·bps·ev·sale_amt·bus_pro·cup_nga·setl_mm + 수집일을 추가 전용 표에 저장(기준 미확인 표시),
  전날과 같으면 "변화 없음" 표시. 판정 스냅샷·백테스트 입력에 나타나지 않는다(그 경로가 이 표·모듈을 읽지 않음).
- 저녁 수집(T-035와 같은 단계) — 설정 financials = true일 때만, 허용 API에 ka10001 추가. 실패는 수집 결과 기록만.
- 축적 시작일: 저장소에서 가장 이른 수집일을 읽어 요약·기록에 남긴다.
"""

from __future__ import annotations

import ast
import json
import sqlite3
from dataclasses import fields as dc_fields, replace
from datetime import UTC, date, datetime, timedelta
from pathlib import Path

import pytest

from tests.scenario.test_t034_investor_probe import make_conn
from tests.scenario.test_t035_investor_flow import EVENING, REPO, FlowInner, World
from trading_system.app import investor_collect
from trading_system.app.investor_collect import EVENING_REST_APIS, FIN_REST_APIS, evening_collect, load_collect_settings
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode
from trading_system.domain.strategy import EvaluationSnapshot, InstrumentView
from trading_system.marketdata.fin_snapshot import FIN_BASIS, FIN_FIELDS, FinParseError, parse_ka10001_fin
from trading_system.storage.repository import Repository

FIN = {"per": "12.5", "eps": "5000", "roe": "8.1", "pbr": "1.2", "bps": "52000", "ev": "4.3", "sale_amt": "2589355",
       "bus_pro": "-65670", "cup_nga": "154870", "setl_mm": "12"}


def fin_world(tmp_path: Path, fin: dict[str, dict] | None = None, **kw) -> World:  # type: ignore[no-untyped-def]
    w = World(tmp_path, inner=FlowInner(fin=fin if fin is not None else {c: FIN for c in ("005930", "000660",
                                                                                          "035420")}), **kw)
    w.settings = replace(w.settings, financials=True)
    w.connect = lambda root, env, clock: make_conn(w.inner, w.clock, allow=FIN_REST_APIS)  # type: ignore[method-assign]
    return w


# --- 해석 ---------------------------------------------------------------------------------------------------------

def test_v265_parse_fields_decimal_text_and_markers() -> None:
    v = parse_ka10001_fin({**FIN, "roe": "N/A", "cur_prc": "+71234"})
    d = v.as_dict()
    assert tuple(d) == FIN_FIELDS
    assert d["per"] == "12.5" and d["bus_pro"] == "-65670" and d["setl_mm"] == "12"
    assert d["roe"] == "N/A" and v.unparsed == ("roe",) and v.missing == ()
    assert "cur_prc" not in d
    v2 = parse_ka10001_fin({k: x for k, x in FIN.items() if k != "ev"})
    assert v2.as_dict()["ev"] is None and v2.missing == ("ev",)
    with pytest.raises(FinParseError):
        parse_ka10001_fin({"stk_cd": "005930"})


# --- 저장 (추가 전용·변화 없음) ----------------------------------------------------------------------------------

def test_v265_store_append_only_with_unchanged_marker(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    v = parse_ka10001_fin(FIN)
    t = datetime(2026, 10, 8, 7, 0, tzinfo=UTC)
    with repo.tx():
        assert repo.record_fin_snapshot("005930", date(2026, 10, 8), v, t, "r1") == "NEW"
    with repo.tx():
        assert repo.record_fin_snapshot("005930", date(2026, 10, 8), v, t, "r2") == "DUPLICATE_DAY"
    with repo.tx():
        assert repo.record_fin_snapshot("005930", date(2026, 10, 12), v, t + timedelta(days=4), "r3") == "UNCHANGED"
    with repo.tx():
        assert repo.record_fin_snapshot("005930", date(2026, 10, 13), parse_ka10001_fin({**FIN, "per": "13"}),
                                        t + timedelta(days=5), "r4") == "NEW"
    rows = repo.select("SELECT collected_day, unchanged, basis, fields FROM fin_snapshot ORDER BY collected_day")
    assert [(r["collected_day"], r["unchanged"]) for r in rows] == [("2026-10-08", 0), ("2026-10-12", 1),
                                                                     ("2026-10-13", 0)]
    assert rows[0]["basis"] == FIN_BASIS and "기준 기간 미확인" in rows[0]["basis"]
    assert json.loads(rows[2]["fields"])["per"] == "13"
    assert repo.fin_snapshot_first_day() == date(2026, 10, 8)
    for sql in ("UPDATE fin_snapshot SET unchanged=0", "DELETE FROM fin_snapshot"):
        with pytest.raises(sqlite3.Error):
            repo.connection.execute(sql)
    repo.close()


def test_first_day_none_when_empty(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.PAPER)
    assert repo.fin_snapshot_first_day() is None
    repo.close()


# --- 저녁 수집 단계 ----------------------------------------------------------------------------------------------

def test_v265_evening_collect_stores_fin_after_flows(tmp_path: Path) -> None:
    w = fin_world(tmp_path)
    assert w.run() == 0, w.out
    apis = [a for _, a, _ in w.inner.log]
    assert apis.count("ka10001") == 3 and apis.index("ka10001") > max(i for i, a in enumerate(apis) if a == "ka10060")
    assert [b for _, a, b in w.inner.log if a == "ka10001"][0] == {"stk_cd": "005930"}
    rows = w.rows("SELECT instrument, collected_day, unchanged FROM fin_snapshot ORDER BY instrument")
    assert [(r["instrument"], r["collected_day"]) for r in rows] == [("000660", "2026-10-08"), ("005930", "2026-10-08"),
                                                                      ("035420", "2026-10-08")]
    meta = json.loads(sorted((w.base / "collect").glob("evening-*.json"))[0].read_text(encoding="utf-8"))
    assert meta["financials"]["ok"] == 3 and meta["financials"]["first_day"] == "2026-10-08"
    assert sorted(meta["allowed_rest_apis"]) == sorted(FIN_REST_APIS)
    assert any("재무 스냅샷(05b)" in line and "축적 시작일 2026-10-08" in line for line in w.out), w.out


def test_v265_fin_failure_recorded_flows_kept(tmp_path: Path) -> None:
    w = fin_world(tmp_path, fin={"005930": FIN, "000660": {"stk_cd": "000660"}, "035420": FIN})
    assert w.run() == 1
    meta = json.loads(sorted((w.base / "collect").glob("evening-*.json"))[0].read_text(encoding="utf-8"))
    assert [f["code"] for f in meta["financials"]["failed"]] == ["000660"]
    assert meta["investor"]["ok"] == 3                                      # 투자자 수집은 그대로
    assert {r["instrument"] for r in w.rows("SELECT instrument FROM fin_snapshot")} == {"005930", "035420"}


def test_financials_off_keeps_t035_allowlist(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    seen: list[frozenset[str]] = []
    monkeypatch.setattr(investor_collect, "connect_paper",
                        lambda root, env, clock, **kw: seen.append(kw["allowed_apis"]) or "거부")
    w = World(tmp_path)
    evening_collect(w.root, {"TRADE_MODE": "PAPER"}, w.clock, w.settings, log=w.out.append)
    evening_collect(w.root, {"TRADE_MODE": "PAPER"}, w.clock, replace(w.settings, financials=True), log=w.out.append)
    assert seen == [EVENING_REST_APIS, FIN_REST_APIS]
    assert FIN_REST_APIS == EVENING_REST_APIS | {"ka10001"}


def test_settings_accept_financials_true(tmp_path: Path) -> None:
    p = tmp_path / "c.toml"
    p.write_text("enabled = true\ncodes_file = \"x.csv\"\nwindow_days = 5\ndeadline = \"17:30\"\nfinancials = true\n",
                 encoding="utf-8")
    assert load_collect_settings(p, tmp_path).financials is True


# --- V-265 판정·백테스트 입력에 없음 ------------------------------------------------------------------------------

DECISION_PATHS = ("src/trading_system/marketdata/store.py", "src/trading_system/app/engine.py",
                  "src/trading_system/app/backtest.py", "src/trading_system/app/wiring.py",
                  "src/trading_system/evaluation/runner.py", "src/trading_system/domain/strategy.py",
                  "src/trading_system/app/validation_tool.py")


@pytest.mark.parametrize("rel", DECISION_PATHS)
def test_v265_decision_and_backtest_paths_never_read_fin(rel: str) -> None:
    text = (REPO / rel).read_text(encoding="utf-8")
    tree = ast.parse(text)
    names = {n.module for n in ast.walk(tree) if isinstance(n, ast.ImportFrom) and n.module}
    assert not any("fin_snapshot" in m for m in names)
    assert "fin_snapshot" not in text and "ka10001_fin" not in text


def test_v265_snapshot_types_have_no_fin_fields() -> None:
    names = {f.name for f in dc_fields(InstrumentView)} | {f.name for f in dc_fields(EvaluationSnapshot)}
    assert not names & {*FIN_FIELDS, "fin", "financials", "fin_snapshot"}


def test_fin_collection_uses_market_priority_query_only(tmp_path: Path) -> None:
    w = fin_world(tmp_path)
    w.run()
    assert not {a for _, a, _ in w.inner.log} - FIN_REST_APIS


def test_evening_fin_first_day_survives_next_run(tmp_path: Path) -> None:
    w = fin_world(tmp_path)
    assert w.run() == 0
    w.clock._now = datetime(2026, 10, 12, 16, 0, tzinfo=KST).astimezone(UTC)   # noqa: SLF001
    assert w.run() == 0
    rows = w.rows("SELECT collected_day, unchanged FROM fin_snapshot WHERE instrument='005930' ORDER BY collected_day")
    assert [(r["collected_day"], r["unchanged"]) for r in rows] == [("2026-10-08", 0), ("2026-10-12", 1)]
    assert any("축적 시작일 2026-10-08" in line for line in w.out)
    assert EVENING.date() == date(2026, 10, 8) and FixedClock
