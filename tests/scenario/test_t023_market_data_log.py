"""T-023 시세 기록 (CP-027 draft, A-071) — PV-10 슬리피지·PV-15 시세 조회 주기 관찰용 진단 기록.

가상 데이터·fake 브로커·합성 응답만 쓴다(네트워크 없음). 검증 항목 V-188~V-192 (CP-027 §4).
- V-188 전송·체결 시점 시세 스냅샷 (메모리 시세 이력에서 그 시각에 알던 시세)
- V-189 시세 조회 통계 (주기·지연·종목 수·응답 필드 이름)
- V-190 기록 실패 격리 (주문 경로·정지 무영향, 경고 1회)
- V-191 저장소 추가 전용 (기존 DB를 열어도 기존 데이터·스키마 버전 유지)
- V-192 검토 보고 PV-10·PV-15 집계
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path

from tests.scenario.test_t020_app import ACCOUNT, DAY, World, kst
from trading_system.domain.clock import FixedClock, to_iso
from trading_system.domain.enums import Mode
from trading_system.domain.market import InstrumentInfo, InstrumentKind, Quote
from trading_system.domain.models import Account
from trading_system.marketdata.kiwoom_feed import KiwoomMarketFeed
from trading_system.marketdata.quote_log import PollStats, QuoteHistory
from trading_system.marketdata.store import MarketDataStore
from trading_system.reporting.paper_review import render_markdown, review
from trading_system.storage.maintenance import migrate, restore_test, schema_version_of
from trading_system.storage.repository import SCHEMA_VERSION, Repository

T0 = datetime(2026, 10, 1, 1, 0, tzinfo=UTC)   # 10:00 KST


def q(price: str, at: datetime, inst: str = "TST-A") -> Quote:
    return Quote(inst, Decimal(price), at, upper_limit=Decimal("13000"), lower_limit=Decimal("7000"))


# --- V-188 (a) 메모리 시세 이력 ------------------------------------------------------------------

def test_v188_history_returns_quote_known_at_that_time() -> None:
    h = QuoteHistory(maxlen=3)
    h.add(q("10000", T0), "ka10001")
    h.add(q("10010", T0 + timedelta(seconds=10)), "ka10001")
    assert h.at_or_before("TST-A", T0 - timedelta(seconds=1)) is None          # 그 시각엔 몰랐음
    got = h.at_or_before("TST-A", T0 + timedelta(seconds=5))
    assert got is not None and got.quote.price == Decimal("10000") and got.source == "ka10001"
    got = h.at_or_before("TST-A", T0 + timedelta(seconds=10))
    assert got is not None and got.quote.price == Decimal("10010")              # 같은 시각 = 알던 것
    assert h.at_or_before("TST-B", T0 + timedelta(seconds=10)) is None
    # 같은 가격·시각(0g 상하한가만 갱신)은 새 항목이 아님
    h.add(q("10010", T0 + timedelta(seconds=10)), "0g")
    assert len(h.entries("TST-A")) == 2
    # 길이 제한: 오래된 것부터 버린다 — 버려진 구간의 시각은 "모름"(None)
    h.add(q("10020", T0 + timedelta(seconds=20)), "ka10001")
    h.add(q("10030", T0 + timedelta(seconds=30)), "ka10001")
    assert len(h.entries("TST-A")) == 3
    assert h.at_or_before("TST-A", T0 + timedelta(seconds=5)) is None


def test_v190_store_quote_path_unaffected_by_history_failure() -> None:
    store = MarketDataStore(None, None)  # type: ignore[arg-type]
    store.register(InstrumentInfo("TST-A", InstrumentKind.STOCK, Decimal("1"), Decimal("1")))

    def boom(*a: object, **k: object) -> None:
        raise RuntimeError("history broken")

    store.history.add = boom  # type: ignore[method-assign]
    store.update_quote(q("10000", T0), source="ka10001")
    assert store.quote("TST-A") is not None and store.quote("TST-A").price == Decimal("10000")  # type: ignore[union-attr]


# --- V-189 시세 조회 통계 ------------------------------------------------------------------------

def test_v189_poll_stats_window_summary() -> None:
    s = PollStats(window=timedelta(minutes=10))
    s.watched = 2
    s.observe("A", True, 0.120, T0, fields=("cur_prc", "upl_pric", "lst_pric"))
    s.observe("B", True, 0.300, T0 + timedelta(seconds=1), fields=("cur_prc", "upl_pric", "lst_pric"))
    s.observe("A", True, 0.200, T0 + timedelta(seconds=3))
    s.observe("B", False, 1.500, T0 + timedelta(seconds=4))
    s.observe("A", True, 0.100, T0 + timedelta(seconds=9))
    assert s.drain(T0 + timedelta(minutes=5)) is None                          # 창이 아직 안 끝남
    out = s.drain(T0 + timedelta(minutes=10))
    assert out is not None
    assert (out["watched"], out["instruments"], out["requests"], out["failures"]) == (2, 2, 5, 1)
    assert (out["latency_ms_p50"], out["latency_ms_max"]) == (200, 1500)
    # 같은 종목의 연속 성공 사이 간격: A 3초·6초 (B는 성공 1회라 간격 없음)
    assert (out["interval_avg_seconds"], out["interval_max_seconds"]) == ("4.500", "6.000")
    assert out["response_fields"] == ["cur_prc", "lst_pric", "upl_pric"]       # 이름만, 정렬
    assert out["window_start"] == T0
    # 다음 창: 필드 집합이 같으면 다시 적지 않는다. 간격은 창을 넘어 이어진다
    s.observe("A", True, 0.100, T0 + timedelta(minutes=10, seconds=9), fields=("cur_prc", "upl_pric", "lst_pric"))
    out2 = s.drain(T0 + timedelta(minutes=20, seconds=9))
    assert out2 is not None and out2["response_fields"] is None and out2["interval_max_seconds"] == "600.000"
    assert s.drain(T0 + timedelta(minutes=40)) is None                         # 관찰 없는 창은 행 없음


class FakeQuerier:
    def __init__(self) -> None:
        self.fail = False

    def query_raw(self, path, api_id, body, list_key, enough=None):  # type: ignore[no-untyped-def]
        from trading_system.broker.base import BrokerQueryError
        if self.fail:
            raise BrokerQueryError("가짜 실패")
        return ({"cur_prc": "+10000", "upl_pric": "13000", "lst_pric": "7000", "stk_nm": "가짜종목명"}, [])


def test_v189_feed_measures_latency_and_records_field_names_only() -> None:
    clock = FixedClock(T0)
    store = MarketDataStore(None, None)  # type: ignore[arg-type]
    ticks = iter([1.0, 1.25, 2.0, 2.5])
    api = FakeQuerier()
    f = KiwoomMarketFeed(api, store, clock, {"TST-A": InstrumentInfo("TST-A", InstrumentKind.STOCK, Decimal("1"),
                                                                      Decimal("1"))},
                         mono=lambda: next(ticks))
    assert f.refresh_quote("TST-A") is not None
    api.fail = True
    assert f.refresh_quote("TST-A") is None
    out = store.poll_stats.drain(T0 + timedelta(minutes=10))
    assert out is not None and out["watched"] == 1
    assert (out["requests"], out["failures"], out["latency_ms_max"]) == (2, 1, 500)
    assert out["response_fields"] == ["cur_prc", "lst_pric", "stk_nm", "upl_pric"]
    assert "가짜종목명" not in repr(out)                                        # 값은 남기지 않는다
    got = store.history.at_or_before("TST-A", T0)
    assert got is not None and got.source == "ka10001" and got.quote.price == Decimal("10000")


# --- V-188 (b) 엔진 배선: 전송·체결 시점 스냅샷 -----------------------------------------------------

def _snaps(w: World) -> list[dict]:
    return w.repo.select("SELECT * FROM quote_snapshot ORDER BY seq")


def _run_entry(w: World) -> None:
    w.engine.start()
    w.resume_k8()
    w.at(9, 5)          # 시세 10000 (받은 시각 09:05:00)
    w.engine.tick()     # 판정 → 게이트 → 전송
    w.engine.tick()     # 체결 통보 반영(fake FILL)


def test_v188_send_and_fill_snapshots_recorded_once(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        _run_entry(w)
        [it] = w.intents()
        assert it["state"] == "FILLED"
        snaps = _snaps(w)
        assert [s["event"] for s in snaps] == ["SEND", "FILL"]
        send, fill = snaps
        attempt = w.repo.attempt_for(it["intent_id"])
        assert send["ref"] == attempt["attempt_id"] and send["event_at"] == attempt["created_at"]
        assert (send["instrument"], send["side"], send["intent_id"], send["account"]) == \
            ("TST-A", "BUY", it["intent_id"], ACCOUNT)
        assert send["quote_price"] == "10000" and send["quote_at"] == to_iso(kst(DAY, 9, 5))
        assert send["quote_age_seconds"] == "0.000" and send["mode"] == Mode.BACKTEST.value
        [f] = w.repo.select("SELECT * FROM fill")
        assert (fill["ref"], fill["event_price"], fill["event_quantity"], fill["event_at"]) == \
            (f["fill_key"], f["price"], f["quantity"], f["at"])
        assert fill["quote_price"] == "10000" and fill["intent_id"] == it["intent_id"]
        # 다음 tick·재시작: 같은 사건을 다시 기록하지 않음, 재시작 전 사건은 새 시세로 소급 기록하지 않음
        w.clock.advance(timedelta(seconds=30))
        w.engine.tick()
        assert len(_snaps(w)) == 2
        w.restart()
        w.engine.start()
        w.engine.tick()
        assert len(_snaps(w)) == 2
    finally:
        w.close()


def test_v188_no_quote_known_is_recorded_as_unknown(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.engine.start()
        w.resume_k8()
        w.at(9, 5)
        w.store.history._data.clear()   # noqa: SLF001 — 이력만 비움(게이트 입력 시세는 그대로)
        w.engine.tick()
        w.engine.tick()
        snaps = _snaps(w)
        assert [s["event"] for s in snaps] == ["SEND", "FILL"]
        assert all(s["quote_price"] is None and s["quote_at"] is None for s in snaps)
    finally:
        w.close()


def test_v189_engine_flushes_poll_stats(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        w.engine.start()
        w.store.poll_stats.watched = 1
        w.store.poll_stats.observe("TST-A", True, 0.05, w.clock.now(), fields=("cur_prc",))
        w.engine.tick()
        assert w.repo.select("SELECT * FROM quote_poll_stat") == []
        w.clock.advance(timedelta(minutes=10))
        w.engine.tick()
        [row] = w.repo.select("SELECT * FROM quote_poll_stat")
        assert (row["watched"], row["requests"], row["failures"], row["latency_ms_max"]) == (1, 1, 0, 50)
        assert row["response_fields"] == '["cur_prc"]' and row["mode"] == Mode.BACKTEST.value
    finally:
        w.close()


# --- V-190 기록 실패 격리 -----------------------------------------------------------------------

def test_v190_recording_failure_does_not_touch_order_path(tmp_path: Path) -> None:
    w = World(tmp_path)
    try:
        def boom(row: dict) -> None:
            raise sqlite3.OperationalError("disk I/O error (주입)")

        w.repo.insert_quote_snapshot = boom  # type: ignore[method-assign]
        _run_entry(w)
        [it] = w.intents()
        assert it["state"] == "FILLED" and len(w.broker.sent) == 1          # 주문은 그대로
        active = set(w.repo.active_halts(ACCOUNT))
        assert not ({"K7", "K8"} & active)                                    # 기록 실패로 정지하지 않음
        for _ in range(3):
            w.clock.advance(timedelta(seconds=1))
            w.engine.tick()
        warn = [e for e in w.repo.audit_events() if e["kind"] == "ALERT" and "시세 기록" in e["summary"]]
        assert len(warn) == 1 and "WARNING" in warn[0]["summary"]            # 실패가 이어져도 경고 1회
        assert not [e for e in w.repo.audit_events() if e["kind"] == "ALERT" and "엔진 훅 오류" in e["summary"]]
        # 회복하면 밀린 사건을 기록한다(기준 위치를 실패 때 올리지 않음)
        del w.repo.insert_quote_snapshot
        w.engine.tick()
        assert [s["event"] for s in _snaps(w)] == ["SEND", "FILL"]
    finally:
        w.close()


# --- V-191 저장소 추가 전용 ----------------------------------------------------------------------

def test_v191_existing_db_gains_tables_without_version_change(tmp_path: Path) -> None:
    p = tmp_path / "old.db"
    r = Repository(p, Mode.BACKTEST)
    r.register_account(Account(ACCOUNT, Mode.BACKTEST, "fake"))
    r.close()
    c = sqlite3.connect(p)   # 이 변경 전 DB 재현: 새 표가 없는 같은 버전
    c.execute("DROP TABLE quote_snapshot")
    c.execute("DROP TABLE quote_poll_stat")
    c.commit()
    before = c.execute("SELECT COUNT(*) FROM account").fetchone()[0]
    c.close()
    r = Repository(p, Mode.BACKTEST)
    names = {n for (n,) in r.connection.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"quote_snapshot", "quote_poll_stat"} <= names
    assert r.connection.execute("SELECT COUNT(*) FROM account").fetchone()[0] == before
    r.close()
    assert schema_version_of(p) == SCHEMA_VERSION == "2"                   # 버전 그대로(추가 전용, D-144)
    assert migrate(p, tmp_path / "bk") == []                                 # 마이그레이션 불필요
    assert restore_test(p, Mode.BACKTEST.value).ok


# --- V-192 검토 보고 ----------------------------------------------------------------------------

def _snap_row(event: str, ref: str, side: str, intent: str, event_price: str | None, quote: str | None,
              tick: str | None, at: datetime, age: str = "1.000") -> dict:
    return {"event": event, "ref": ref, "account": ACCOUNT, "instrument": "TST-A", "side": side, "intent_id": intent,
            "event_at": at, "event_price": event_price, "event_quantity": "1" if event == "FILL" else None,
            "quote_price": quote, "quote_at": at - timedelta(seconds=1) if quote else None,
            "quote_source": "ka10001" if quote else None, "quote_age_seconds": age if quote else None,
            "tick": tick, "recorded_at": at}


def test_v192_review_reports_slippage_in_ticks_and_poll_stats(tmp_path: Path) -> None:
    p = tmp_path / "trading.db"
    r = Repository(p, Mode.BACKTEST)
    with r.tx():
        # 매수: 전송 시 10000 → 체결 10010 (틱 5 → 불리 +2틱), 체결 시각 시세 10005 → +1틱
        r.insert_quote_snapshot(_snap_row("SEND", "a1", "BUY", "i1", "0", "10000", "5", T0))
        r.insert_quote_snapshot(_snap_row("FILL", "f1", "BUY", "i1", "10010", "10005", "5", T0 + timedelta(seconds=2)))
        # 매도: 전송 시 10000 → 체결 9990 (불리 +2틱), 체결 시각 시세 모름
        r.insert_quote_snapshot(_snap_row("SEND", "a2", "SELL", "i2", "0", "10000", "5", T0))
        r.insert_quote_snapshot(_snap_row("FILL", "f2", "SELL", "i2", "9990", None, "5", T0 + timedelta(seconds=2)))
        r.insert_poll_stat({"window_start": T0, "window_end": T0 + timedelta(minutes=10), "watched": 20,
                            "instruments": 20, "requests": 400, "failures": 2, "latency_ms_p50": 180,
                            "latency_ms_max": 900, "interval_avg_seconds": "24.500", "interval_max_seconds": "25.000",
                            "response_fields": ["cur_prc", "upl_pric"], "recorded_at": T0 + timedelta(minutes=10)})
    r.close()
    rv = review(p)
    assert [(x["side"], x["slip_send_ticks"], x["slip_fill_ticks"]) for x in rv.slippage] == \
        [("BUY", "2", "1"), ("SELL", "2", "-")]
    assert rv.poll_stats[0]["over_limit"] is True                             # 25초 × 3 = 75초 > 60초 (D-095)
    md = render_markdown(rv)
    assert "PV-10" in md and "PV-15" in md and "cur_prc, upl_pric" in md
    assert "시세 이력을 저장하지 않음" not in md


def test_v192_review_of_db_without_tables_says_no_data(tmp_path: Path) -> None:
    p = tmp_path / "old.db"
    Repository(p, Mode.BACKTEST).close()
    c = sqlite3.connect(p)
    c.execute("DROP TABLE quote_snapshot")
    c.execute("DROP TABLE quote_poll_stat")
    c.commit()
    c.close()
    rv = review(p)                                   # 읽기 전용 — 표를 만들지 않는다
    assert rv.slippage == [] and rv.poll_stats == [] and rv.market_log is False
    assert "시세 기록 표 없음" in render_markdown(rv)
    c = sqlite3.connect(p)
    assert c.execute("SELECT COUNT(*) FROM sqlite_master WHERE name='quote_snapshot'").fetchone()[0] == 0
    c.close()


def test_v192_review_tolerates_missing_tick(tmp_path: Path) -> None:
    p = tmp_path / "trading.db"
    r = Repository(p, Mode.BACKTEST)
    with r.tx():
        r.insert_quote_snapshot(_snap_row("SEND", "a1", "BUY", "i1", "0", "10000", None, T0))
        r.insert_quote_snapshot(_snap_row("FILL", "f1", "BUY", "i1", "10010", "10005", None, T0))
    r.close()
    [x] = review(p).slippage
    assert (x["slip_send"], x["slip_send_ticks"], x["slip_fill"]) == ("10", "-", "5")
