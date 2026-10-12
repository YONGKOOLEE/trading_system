"""T-041 U-B 사람 목록 운용·listed_at (CP-034 D-198, A-092 Q-8 / V-274·V-275·V-276).

모든 종목·목록·가격은 가상이다. 네트워크·브로커 연결 없음(fake 브로커).
"""

from __future__ import annotations

import json
from dataclasses import replace
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.support.engine_world import ACCOUNT, DAY, EngineWorld, kst
from tests.support.sample_strategies import CAPS_ALL_SUPPORTED, CrossStrategy, cross_spec
from tests.support.universe_strategies import LIST_ID, UbIncrease, UbProbe, ub_spec
from trading_system.config.strategy_registry import validate_spec
from trading_system.domain.clock import KST
from trading_system.domain.enums import Mode, OrderType, RequestType
from trading_system.domain.strategy import Bar, EvaluationSnapshot, InstrumentView, PositionView
from trading_system.domain.universe import (
    MAX_WATCHED, ListEntry, ListVersion, content_hash, listed_since, parse_list_file, select_version,
)
from trading_system.evaluation.runner import run_evaluation
from trading_system.storage.repository import Repository
from trading_system.strategy.base import make_signal

A, B, C = "000010", "000020", "00003A"


def _ver(n: int, at: datetime, codes: tuple[str, ...]) -> ListVersion:
    entries = tuple(ListEntry(c) for c in codes)
    return ListVersion(LIST_ID, n, content_hash(entries), at, "LOCAL", codes)


def submit(repo: Repository, codes: tuple[str, ...], at: datetime, list_id: str = LIST_ID) -> int:
    entries = tuple(ListEntry(c) for c in codes)
    with repo.tx():
        return repo.insert_universe_version(list_id, entries, content_hash(entries), at, "LOCAL", "tester")


# --- V-274 파일 형식·제출 --------------------------------------------------------------------------

def test_v274_parse_valid_file_with_bom_memo_and_blank_lines() -> None:
    data = "﻿005930,삼성전자\r\n\r\n0001A0\n000660 , 메모, 쉼표 포함\n".encode()
    entries, problems = parse_list_file(data)
    assert problems == []
    assert entries == (ListEntry("005930", "삼성전자"), ListEntry("0001A0", ""), ListEntry("000660", "메모, 쉼표 포함"))


@pytest.mark.parametrize("text, needle", [
    ("05930\n", "형식"), ("0059300\n", "형식"), ("00593a\n", "형식"), ("code,memo\n005930\n", "형식"),
    ("005930\n005930,중복\n", "중복"),
])
def test_v274_parse_rejects_format_and_duplicates(text: str, needle: str) -> None:
    entries, problems = parse_list_file(text.encode())
    assert problems and any(needle in p for p in problems)


def test_v274_parse_rejects_non_utf8() -> None:
    _, problems = parse_list_file("005930,삼성".encode("cp949"))
    assert problems and "UTF-8" in problems[0]


def test_v274_hash_changes_with_content() -> None:
    a = content_hash((ListEntry("005930", ""),))
    assert a == content_hash((ListEntry("005930", ""),)) and len(a) == 64
    assert a != content_hash((ListEntry("005930", "메모"),)) != content_hash((ListEntry("000660", ""),))


def test_v274_repository_versions_append_only_and_local_only(tmp_path: Path) -> None:
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    try:
        t0 = kst(DAY, 8, 0)
        assert submit(repo, (A, B), t0) == 1
        assert submit(repo, (B,), t0 + timedelta(minutes=1)) == 2
        vs = repo.universe_versions(LIST_ID)
        assert [(v.version, v.codes, v.channel) for v in vs] == [(1, (A, B), "LOCAL"), (2, (B,), "LOCAL")]
        assert vs[0].submitted_at == t0
        entries = (ListEntry(A),)
        with pytest.raises(ValueError, match="로컬"):   # 원격·대화 제출 불가 (Q-8)
            with repo.tx():
                repo.insert_universe_version(LIST_ID, entries, content_hash(entries), t0, "REMOTE", "x")
        with pytest.raises(Exception):                   # 추가 전용 (UPDATE·DELETE 금지)
            with repo.tx():
                repo.select("DELETE FROM universe_list_version")
        assert len(repo.universe_versions(LIST_ID)) == 2
    finally:
        repo.close()


def test_v274_existing_db_gets_table_and_schema_version_kept(tmp_path: Path) -> None:
    import sqlite3

    from trading_system.storage.repository import SCHEMA_VERSION
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)
    repo.connection.execute("DROP TABLE universe_list_version")   # 이 표가 없던 기존 저장소를 흉내
    repo.close()
    repo = Repository(tmp_path / "t.db", Mode.BACKTEST)            # 열 때 생성 (A-086 ③ 방식)
    try:
        assert repo.universe_versions(LIST_ID) == []
        assert SCHEMA_VERSION == "2"
        con = sqlite3.connect(tmp_path / "t.db")
        assert con.execute("SELECT value FROM meta WHERE key='schema_version'").fetchone()[0] == "2"
        con.close()
    finally:
        repo.close()


def test_v274_remote_ops_command_is_rejected(tmp_path: Path) -> None:
    w = EngineWorld(tmp_path, [(UbProbe(), {"qty": 1}, (time(9, 5),))], [A, B])
    try:
        w.start_and_resume()
        cid = w.command("UNIVERSE_SUBMIT", {"list": LIST_ID, "codes": [A]}, channel="REMOTE")
        w.engine.tick()
        row = w.repo.command(cid)
        assert row["status"] != "DONE" and "알 수 없는 명령" in (row["result"] or "")
        assert w.repo.universe_versions(LIST_ID) == []
    finally:
        w.close()


# --- 제출 도구 (scripts/universe.py) ---------------------------------------------------------------

APP_TOML = """
[app]
account = "KR-PAPER-01"
loop_seconds = "0.5"
reconcile_minutes = 1
sntp_servers = ["a:123"]

{instruments}

[[strategies]]
module = "tests.support.universe_strategies:UbProbe"
params = "config/strategies/t-ub.toml"
eval_times = ["09:05"]
"""


def _root(tmp_path: Path, codes: list[str]) -> Path:
    root = tmp_path / "repo"
    (root / "config" / "strategies").mkdir(parents=True)
    inst = "\n".join(f'[[instruments]]\ncode = "{c}"\nkind = "STOCK"\nleverage = "1"\nlot_size = "1"\n'
                     for c in codes)
    (root / "config" / "app.paper.toml").write_text(APP_TOML.format(instruments=inst), encoding="utf-8")
    (root / "config" / "strategies" / "t-ub.toml").write_text('strategy = "t-ub"\nversion = 1\n[params]\nqty = 1\n',
                                                               encoding="utf-8")
    return root


def _codes(n: int) -> list[str]:
    return [f"{100000 + i:06d}" for i in range(n)]


def _tool(root: Path, *args: str) -> int:
    from trading_system.app.universe_tool import universe_main
    return universe_main(list(args), root=root)


def _paper_repo(root: Path) -> Repository:
    return Repository(root / "var" / "paper" / "trading.db", Mode.PAPER)


def test_tool_submit_valid_records_version_hash_channel_audit(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _root(tmp_path, _codes(3))
    f = tmp_path / "list.csv"
    f.write_bytes("﻿100000,첫째\n100001\n".encode())
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(f)) == 0
    repo = _paper_repo(root)
    try:
        [v] = repo.universe_versions(LIST_ID)
        assert (v.version, v.codes, v.channel) == (1, ("100000", "100001"), "LOCAL")
        assert v.content_hash == content_hash((ListEntry("100000", "첫째"), ListEntry("100001", "")))
        [ev] = [e for e in repo.audit_events() if e["kind"] == "UNIVERSE_SUBMITTED"]
        s = json.loads(ev["summary"])
        assert (s["list"], s["version"], s["hash"], s["count"], s["channel"]) == \
            (LIST_ID, 1, v.content_hash, 2, "LOCAL")
    finally:
        repo.close()
    assert "버전 1" in capsys.readouterr().out


@pytest.mark.parametrize("content, needle", [
    ("1000001\n", "형식"), ("100000\n100000\n", "중복"), ("999999\n", "모르는 종목"),
])
def test_tool_rejects_invalid_and_keeps_previous_version(tmp_path: Path, content: str, needle: str,
                                                         capsys) -> None:  # type: ignore[no-untyped-def]
    root = _root(tmp_path, _codes(3))
    good, bad = tmp_path / "good.csv", tmp_path / "bad.csv"
    good.write_text("100000\n", encoding="utf-8")
    bad.write_text(content, encoding="utf-8")
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(good)) == 0
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(bad)) == 1
    out = capsys.readouterr().out
    assert "거부" in out and needle in out
    repo = _paper_repo(root)
    try:
        assert [v.codes for v in repo.universe_versions(LIST_ID)] == [("100000",)]   # 이전 버전 유지
        assert [e for e in repo.audit_events() if e["kind"] == "UNIVERSE_SUBMIT_REJECTED"]
    finally:
        repo.close()


def test_tool_rejects_watch_set_over_15_including_holdings(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    codes = _codes(17)
    root = _root(tmp_path, codes)
    f = tmp_path / "l.csv"
    f.write_text("\n".join(codes[:MAX_WATCHED]) + "\n", encoding="utf-8")
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(f)) == 0   # 15 = 통과
    repo = _paper_repo(root)
    repo.set_position("KR-PAPER-01", codes[16], Decimal("1"), Decimal("1000"))   # 목록 밖 보유 1종목 → 16
    repo.close()
    f.write_text("\n".join(codes[1:MAX_WATCHED]) + "\n", encoding="utf-8")       # 14 + 보유 1 = 15 통과
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(f)) == 0
    f.write_text("\n".join(codes[:MAX_WATCHED]) + "\n", encoding="utf-8")         # 15 + 보유 1 = 16 거부
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(f)) == 1
    assert "15" in capsys.readouterr().out
    repo = _paper_repo(root)
    try:
        assert [len(v.codes) for v in repo.universe_versions(LIST_ID)] == [15, 14]
    finally:
        repo.close()


def test_tool_rejects_live_and_unknown_mode(tmp_path: Path, capsys) -> None:  # type: ignore[no-untyped-def]
    root = _root(tmp_path, _codes(1))
    f = tmp_path / "l.csv"
    f.write_text("100000\n", encoding="utf-8")
    assert _tool(root, "submit", "--mode", "LIVE", "--list", LIST_ID, "--file", str(f)) == 2
    assert _tool(root, "submit", "--mode", "live2", "--list", LIST_ID, "--file", str(f)) == 2
    assert not (root / "var" / "live").exists()
    assert "거부" in capsys.readouterr().out


def test_tool_same_content_makes_no_new_version_and_empty_list_allowed(tmp_path: Path) -> None:
    root = _root(tmp_path, _codes(2))
    f = tmp_path / "l.csv"
    f.write_text("100000\n", encoding="utf-8")
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(f)) == 0
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(f)) == 0
    f.write_text("", encoding="utf-8")                 # 빈 목록 = 신규 진입 없음 (D-198 (5)) — 유효한 버전
    assert _tool(root, "submit", "--mode", "PAPER", "--list", LIST_ID, "--file", str(f)) == 0
    assert _tool(root, "show", "--mode", "PAPER", "--list", LIST_ID) == 0
    repo = _paper_repo(root)
    try:
        assert [v.codes for v in repo.universe_versions(LIST_ID)] == [("100000",), ()]
    finally:
        repo.close()


# --- 명세 검증 ----------------------------------------------------------------------------------------

def test_spec_universe_list_required_for_ub_only() -> None:
    assert validate_spec(ub_spec(), CAPS_ALL_SUPPORTED, None, Mode.BACKTEST) == []
    p = validate_spec(ub_spec(universe_list=None), CAPS_ALL_SUPPORTED, None, Mode.BACKTEST)
    assert any("universe_list" in x for x in p)
    p = validate_spec(ub_spec(universe_list="bad id!"), CAPS_ALL_SUPPORTED, None, Mode.BACKTEST)
    assert any("universe_list" in x for x in p)
    p = validate_spec(cross_spec(universe_list="x"), CAPS_ALL_SUPPORTED, None, Mode.BACKTEST)
    assert any("universe_list" in x for x in p)
    assert cross_spec().spec_hash() == cross_spec(universe_list=None).spec_hash()


# --- V-275 적용 시점·제외·빈 목록 (엔진) -----------------------------------------------------------

def test_v275_select_version_uses_only_submissions_10_minutes_before() -> None:
    t = kst(DAY, 9, 5)
    vs = [_ver(1, t - timedelta(minutes=30), (A,)), _ver(2, t - timedelta(minutes=10), (B,)),
          _ver(3, t - timedelta(minutes=9, seconds=59), (C,))]
    assert select_version(vs, t).version == 2              # 경계(T − 10분 정각)는 사용, 그 뒤 제출분은 미사용
    assert select_version(vs[:1] + vs[2:], t).version == 1
    assert select_version(vs[2:], t) is None


def _world(tmp_path: Path, strat: UbProbe | None = None, times: tuple[time, ...] = (time(9, 5), time(10, 5))):
    return EngineWorld(tmp_path, [(strat or UbProbe(), {"qty": 1}, times)], [A, B, C])


def _eval_summaries(w: EngineWorld) -> list[dict]:
    return [json.loads(e["summary"]) for e in w.audits("EVALUATION")]


def test_v275_engine_uses_list_applied_at_t_minus_10_and_records_version(tmp_path: Path) -> None:
    strat = UbProbe()
    w = _world(tmp_path, strat)
    try:
        submit(w.repo, (A,), kst(DAY, 8, 50))
        submit(w.repo, (A, B), kst(DAY, 8, 56))             # 09:05 − 10분 = 08:55 이후 → 이 판정에 미사용
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        assert [(r["request_type"], r["instrument"]) for r in w.intents()] == [("ENTRY", A)]
        [s] = _eval_summaries(w)
        assert s["universe_list"]["list"] == LIST_ID and s["universe_list"]["version"] == 1
        assert s["universe_list"]["hash"] == w.repo.universe_versions(LIST_ID)[0].content_hash
        assert [v.instrument for v in strat.snapshots[0].instruments] == [A]
    finally:
        w.close()


def test_v275_removed_holding_keeps_exit_judgement_and_gets_no_entry(tmp_path: Path) -> None:
    w = _world(tmp_path)
    try:
        submit(w.repo, (A, B), kst(DAY, 8, 0))
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        w.at(9, 6)
        w.engine.tick()
        assert w.repo.position(ACCOUNT, A).quantity == 1 and w.repo.position(ACCOUNT, B).quantity == 1
        submit(w.repo, (B, C), kst(DAY, 9, 30))            # A 제외, C 추가
        w.at(10, 5)
        w.engine.tick()
        rows = [(r["request_type"], r["instrument"]) for r in w.intents()]
        # A: 목록에서 빠졌지만 이 전략 보유 → 청산 판정 유지(자동 청산이 아니라 전략 규칙의 청산 신호)
        assert ("EXIT", A) in rows and ("EXIT", B) in rows and ("ENTRY", C) in rows
        assert rows.count(("ENTRY", A)) == 1                # 처음 진입 한 번뿐
    finally:
        w.close()


def test_v275_removed_holding_increase_is_dropped(tmp_path: Path) -> None:
    w = _world(tmp_path, UbIncrease())
    try:
        submit(w.repo, (A,), kst(DAY, 8, 0))
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        w.at(9, 6)
        w.engine.tick()
        submit(w.repo, (B,), kst(DAY, 9, 30))
        w.at(10, 5)
        w.engine.tick()
        rows = [(r["request_type"], r["instrument"]) for r in w.intents()]
        assert ("INCREASE", A) not in rows and ("ENTRY", B) in rows
        assert _eval_summaries(w)[-1]["dropped"] == 1
    finally:
        w.close()


def test_runner_entry_universe_drops_entry_and_increase_only() -> None:
    spec = ub_spec()
    as_of = kst(DAY, 9, 5)
    bars = (Bar(DAY - timedelta(days=1), Decimal(1), Decimal(1), Decimal(1), Decimal(1), Decimal(1)),)
    pos = PositionView(Decimal("2"), Decimal("1"), 0, True)
    snap = EvaluationSnapshot("ev-1", "M1", ACCOUNT, as_of, Decimal("1000000"),
                              (InstrumentView(A, bars, position=pos), InstrumentView(B, bars, position=pos),
                               InstrumentView(C, bars)))

    class S:
        def __init__(self) -> None:
            self.spec = spec

        def evaluate(self, snapshot, params):  # type: ignore[no-untyped-def]
            mk = lambda i, r, k, q: make_signal(spec, snapshot, i, r, k, Decimal(q), OrderType.MARKET,  # noqa: E731
                                                Decimal(1), timedelta(minutes=5))
            return [mk(A, "INC-1", RequestType.INCREASE, "1"), mk(B, "EXIT-1", RequestType.EXIT, "2"),
                    mk(C, "ENTRY-1", RequestType.ENTRY, "1")]

    r = run_evaluation(S(), snap, {}, entry_universe=frozenset({C}))
    assert [(s.kind, s.instrument) for s in r.signals] == [(RequestType.EXIT, B), (RequestType.ENTRY, C)]
    assert [d.instrument for d in r.dropped] == [A] and "D-198" in r.dropped[0].reason
    r = run_evaluation(S(), snap, {}, entry_universe=frozenset())
    assert [(s.kind, s.instrument) for s in r.signals] == [(RequestType.EXIT, B)]


@pytest.mark.parametrize("empty", [True, False])
def test_v275_empty_or_missing_list_no_entries_exits_continue_warn_once(tmp_path: Path, empty: bool) -> None:
    w = _world(tmp_path, times=(time(9, 5), time(10, 5), time(11, 5)))
    try:
        submit(w.repo, (A,), kst(DAY, 8, 0))
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        w.at(9, 6)
        w.engine.tick()
        if empty:
            submit(w.repo, (), kst(DAY, 9, 30))
        else:   # 목록 없음: 다른 목록을 쓰는 전략으로 바꾼 것과 같다 — 버전이 하나도 없는 목록 ID
            w.engine.strategies[0].strategy.spec = ub_spec(universe_list="no-such-list")  # type: ignore[misc]
            w.repo.set_position(ACCOUNT, A, Decimal("1"), Decimal("10000"))
        w.at(10, 5)
        w.engine.tick()
        w.at(11, 5)
        w.engine.tick()
        later = [(r["request_type"], r["instrument"]) for r in w.intents()][1:]
        assert ("ENTRY", B) not in later and ("ENTRY", C) not in later and ("ENTRY", A) not in later
        if empty:
            assert ("EXIT", A) in later                     # 보유 청산 판정은 계속
        warns = [e for e in w.repo.audit_events() if e["kind"] == "ALERT" and "U-B 목록" in e["summary"]]
        assert len(warns) == 1 and "WARNING" in warns[0]["summary"]   # 하루 1회
    finally:
        w.close()


# --- V-276 listed_at ------------------------------------------------------------------------------

def test_v276_listed_at_is_start_of_continuous_listing_and_resets_on_relisting() -> None:
    t = [kst(DAY, 8, i) for i in range(4)]
    vs = [_ver(1, t[0], (A,)), _ver(2, t[1], (A, B)), _ver(3, t[2], (B,)), _ver(4, t[3], (A, B))]
    assert listed_since(vs, vs[3], A) == t[3].astimezone(KST)          # 빠졌다 재등재 → 새로 시작
    assert listed_since(vs, vs[3], B) == t[1].astimezone(KST)          # 연속 등재 시작 버전
    assert listed_since(vs, vs[1], A) == t[0].astimezone(KST)
    assert listed_since(vs, vs[2], A) is None                          # 적용 버전에 없음
    assert listed_since(vs, vs[3], A).tzinfo is not None


def test_v276_engine_snapshot_has_listed_at_in_kst(tmp_path: Path) -> None:
    strat = UbProbe()
    w = _world(tmp_path, strat)
    try:
        submit(w.repo, (A,), kst(DAY - timedelta(days=1), 15, 0))
        submit(w.repo, (A, B), kst(DAY, 8, 0))
        w.start_and_resume()
        w.at(9, 5)
        w.engine.tick()
        views = {v.instrument: v for v in strat.snapshots[0].instruments}
        assert views[A].listed_at == datetime(2026, 9, 30, 15, 0, tzinfo=KST)
        assert views[B].listed_at == datetime(2026, 10, 1, 8, 0, tzinfo=KST)
        assert views[A].listed_at.utcoffset() == timedelta(hours=9)
    finally:
        w.close()


# --- V-276 백테스트 목록 이력 재현 ----------------------------------------------------------------

def _bt_config(tmp_path: Path, history: dict | None, run_id: str = "u1"):  # type: ignore[no-untyped-def]
    from tests.scenario.test_t009_backtest import CAL, SETTINGS, path
    from tests.support.gate_fakes import scenario_limits
    from trading_system.app.backtest import BacktestConfig
    from trading_system.config.strategy_params import validate_params
    from trading_system.domain.market import InstrumentInfo, InstrumentKind
    data = {"TST-A": path([9000, 9100, 9200]), "TST-B": path([10000, 10100]), "TST-C": path([8000, 8100, 8050])}
    strat = UbProbe()
    return BacktestConfig(
        run_id=run_id, strategy=strat, params=validate_params(strat.spec, {"qty": 1}), caps=CAPS_ALL_SUPPORTED,
        account_limits=scenario_limits(l7_max_order_amount=Decimal("10000000")), gate_settings=SETTINGS,
        calendar=CAL, ticks=None, slippage_tick=Decimal("10"), data=data,
        infos={i: InstrumentInfo(i, InstrumentKind.STOCK, Decimal("1"), Decimal("1")) for i in data},
        start=date(2026, 1, 5), end=date(2026, 1, 30), initial_cash=Decimal("10000000"),
        out_dir=tmp_path / "bt", data_version="v", code_commit="t", final_from=date(2027, 1, 1),
        universe_history=history)


def test_v276_backtest_reproduces_list_per_day_without_lookahead(tmp_path: Path) -> None:
    from trading_system.app.backtest import run_backtest
    hist = {date(2026, 1, 5): ("TST-A",), date(2026, 1, 12): ("TST-A", "TST-B"), date(2026, 1, 20): ("TST-C",)}
    base = run_backtest(_bt_config(tmp_path, hist, "h1"))
    assert base.universe_point_in_time is True
    entered = {(k[:10], s[2]) for k, sigs in base.signals_by_eval.items() for s in sigs if s[1] == "ENTRY"}
    assert all(i == "TST-A" for d, i in entered if d < "2026-01-12")
    assert any(i == "TST-B" for d, i in entered if "2026-01-12" <= d < "2026-01-20")
    assert all(i == "TST-C" for d, i in entered if d >= "2026-01-20")
    # 교란: 판정일 d 이후 목록을 바꿔도 d까지의 신호는 같다
    d = date(2026, 1, 14)
    moved = {**{k: v for k, v in hist.items() if k <= d}, date(2026, 1, 15): ("TST-B", "TST-C"),
             date(2026, 1, 20): ("TST-A",)}
    other = run_backtest(_bt_config(tmp_path, moved, "h2"))
    keep = lambda r: {k: v for k, v in r.signals_by_eval.items() if k[:10] <= d.isoformat()}  # noqa: E731
    assert keep(base) == keep(other)
    assert base.signals_by_eval != other.signals_by_eval   # 교란이 실제로 뒤 날짜에 영향을 줬는지(검사 유효성)


def test_v276_backtest_without_history_marks_not_point_in_time(tmp_path: Path) -> None:
    from trading_system.app.backtest import run_backtest
    from trading_system.app.validation import universe_after_the_fact
    r = run_backtest(_bt_config(tmp_path, None, "n1"))
    assert r.universe_point_in_time is False and r.signals            # 데이터 전 종목을 사후 선택 목록으로
    summary = json.loads((tmp_path / "bt" / "n1" / "summary.json").read_text(encoding="utf-8"))
    assert summary["universe"]["point_in_time"] is False and "C-7" in summary["universe"]["note"]
    cfg = _bt_config(tmp_path, None)
    assert universe_after_the_fact(cfg, declared_point_in_time=True) is True     # U-B 이력 없음 → C-7 표시
    assert universe_after_the_fact(replace(cfg, universe_history={date(2026, 1, 5): ("TST-A",)}),
                                   declared_point_in_time=False) is False
    cross = replace(cfg, strategy=CrossStrategy())
    assert universe_after_the_fact(cross, declared_point_in_time=False) is True  # U-A는 기존 선언 그대로
    assert universe_after_the_fact(cross, declared_point_in_time=True) is False


def test_load_universe_history_csv(tmp_path: Path) -> None:
    from trading_system.app.main import load_universe_history
    from trading_system.config.errors import ConfigError
    f = tmp_path / "h.csv"
    f.write_text("﻿date,code\n2026-01-05,005930\n2026-01-05,000660\n2026-01-12,\n", encoding="utf-8")
    assert load_universe_history(f) == {date(2026, 1, 5): ("000660", "005930"), date(2026, 1, 12): ()}
    f.write_text("date,code\n2026-01-05,5930\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_universe_history(f)
    f.write_text("date,code\n2026-01-05,005930\n2026-01-05,005930\n", encoding="utf-8")
    with pytest.raises(ConfigError):
        load_universe_history(f)


def test_ub_snapshot_time_naive_guard() -> None:
    with pytest.raises(ValueError):
        select_version([], datetime(2026, 10, 1, 9, 5))   # 시간대 없는 시각 금지
    assert select_version([], datetime(2026, 10, 1, 9, 5, tzinfo=UTC)) is None
