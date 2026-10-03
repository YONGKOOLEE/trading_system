"""T-015 시작 점검·키움 모의 접속(조회만) — 05-plan T-015, 03 D-049·D-066, 04 PV-06·PV-11 (네트워크 없이, 합성 응답).

- 사용할 조회 API를 1회씩 부르고 모의 미지원(8104)을 찾는다. 토큰은 발급 후 폐기한다.
- 점검 경로는 주문 API를 전송 전에 막는다(주문 0건).
- 보고서에는 앱키·시크릿·토큰·계좌번호를 남기지 않는다.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from datetime import UTC, date, datetime, timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.test_t014_kiwoom import ALIAS, adapter, buy
from tests.support.kiwoom_sim import ACCOUNT_NO, APP_KEY, SECRET, Scripted, Sim, _md_init
from trading_system.app.preflight import (
    ORDER_API_IDS, QUERY_APIS, GuardedHttp, preflight_main, run_preflight)
from trading_system.broker.kiwoom.http import KiwoomHttp
from trading_system.domain.clock import KST, FixedClock
from trading_system.domain.enums import Mode, SubmitOutcome
from trading_system.domain.market import InstrumentInfo, InstrumentKind
from trading_system.marketdata.calendar import KrxCalendar
from trading_system.marketdata.store import MarketDataStore
from trading_system.storage.repository import Repository

CODE = "005930"
T0 = datetime(2026, 10, 2, 1, 0, tzinfo=UTC)  # 10:00 KST


@pytest.fixture
def clock() -> FixedClock:
    return FixedClock(T0)


@pytest.fixture
def sim(clock: FixedClock) -> Iterator[Sim]:
    s = Sim(now=lambda: clock.now().astimezone(KST).replace(tzinfo=None))
    _md_init(s)
    d, rows = date(2026, 10, 1), []
    while len(rows) < 30:
        if d.weekday() < 5:
            p = 70000 + len(rows)
            rows.append({"dt": d.strftime("%Y%m%d"), "open_pric": str(p), "high_pric": str(p + 500),
                         "low_pric": str(p - 500), "cur_prc": f"+{p}", "trde_qty": "1000", "pred_pre": "+0"})
        d -= timedelta(days=1)
    s.daily[CODE] = rows
    s.info[CODE] = {"cur_prc": "+70000", "upl_pric": "+91000", "lst_pric": "-49000"}
    yield s
    s.close()


def guarded(sim: Sim, clock: FixedClock):  # type: ignore[no-untyped-def]
    g = GuardedHttp(KiwoomHttp(sim.url, 1.0, 0.5))
    a = adapter(sim, clock)
    a.http = g
    a.tokens._http = g  # noqa: SLF001 — 시험: 토큰 발급·폐기도 같은 경계를 지나게
    return a, g


def store() -> MarketDataStore:
    return MarketDataStore(KrxCalendar({}), None)


def repo(tmp_path: Path) -> Repository:
    return Repository(tmp_path / "var" / "paper" / "trading.db", Mode.PAPER)


def run(sim: Sim, clock: FixedClock, tmp_path: Path):  # type: ignore[no-untyped-def]
    a, g = guarded(sim, clock)
    r = repo(tmp_path)
    try:
        return run_preflight(a, g, store(), r, clock, ALIAS, CODE), a, g
    finally:
        r.close()


# --- 정상 ---------------------------------------------------------------------------------------

def test_preflight_calls_every_query_api_once_and_sends_no_order(sim: Sim, clock: FixedClock,
                                                                   tmp_path: Path) -> None:
    rep, _, g = run(sim, clock, tmp_path)
    assert rep.ok, rep.failures
    called = {api for api, _, _ in sim.log}
    assert set(QUERY_APIS) <= called                       # PV-06: 사용 API 전부 호출
    assert not called & ORDER_API_IDS and g.blocked == []  # 주문 0건
    assert {s.api_id for s in rep.apis if s.ok} >= set(QUERY_APIS)
    assert rep.unsupported == []
    assert rep.reconcile == "MATCH"


def test_token_issued_then_revoked_and_expiry_recorded(sim: Sim, clock: FixedClock, tmp_path: Path) -> None:
    rep, _, _ = run(sim, clock, tmp_path)
    apis = [api for api, _, _ in sim.log]
    assert apis[0] == "au10001" and apis[-1] == "au10002"   # D-049: 시작 발급, 종료 폐기
    assert sim.valid_tokens == set()
    assert rep.token_expires_at is not None and rep.token_revoked     # PV-11 관찰 기록


def test_start_reconcile_records_external_holding_as_mismatch(sim: Sim, clock: FixedClock,
                                                              tmp_path: Path) -> None:
    sim.positions[CODE] = (Decimal("3"), Decimal("70000"))
    rep, _, _ = run(sim, clock, tmp_path)
    assert rep.reconcile == "MISMATCH" and not rep.ok
    assert any("대조 불일치 1건" in f for f in rep.failures)


def test_market_data_api_error_fails_even_though_feed_swallows_it(sim: Sim, clock: FixedClock,
                                                                  tmp_path: Path) -> None:
    # 시장 데이터 공급자는 조회 실패를 삼키고 "모름"으로 둔다 → 시작 점검은 HTTP 기록으로 잡아야 한다
    sim.script("ka10099", Scripted("body", body={"return_code": 1902, "return_msg": "가상 업무 오류"}))
    rep, _, _ = run(sim, clock, tmp_path)
    assert not rep.ok and rep.unsupported == []
    assert any(f.startswith("ka10099:") for f in rep.failures)


# --- 오류 ---------------------------------------------------------------------------------------

def test_mock_unsupported_8104_fails_and_names_api(sim: Sim, clock: FixedClock, tmp_path: Path) -> None:
    sim.script("ka10054", Scripted("body", body={"return_code": 8104, "return_msg": "모의투자를 지원하지 않습니다"}))
    rep, a, _ = run(sim, clock, tmp_path)
    assert not rep.ok and rep.unsupported == ["ka10054"]
    assert ("MOCK_UNSUPPORTED" in {k for k, _ in a.critical})


def test_token_failure_stops_before_queries(sim: Sim, clock: FixedClock, tmp_path: Path) -> None:
    sim.token_fail = 10
    rep, _, _ = run(sim, clock, tmp_path)
    assert not rep.ok and rep.failures and "토큰" in rep.failures[0]
    assert {api for api, _, _ in sim.log} <= {"au10001", "au10002"}


def test_mode_mismatch_code_is_reported(sim: Sim, clock: FixedClock, tmp_path: Path) -> None:
    sim.script("kt00005", Scripted("body", body={"return_code": 8030, "return_msg": "[8030:실전/모의 투자구분 불일치]"}))
    rep, a, _ = run(sim, clock, tmp_path)
    assert not rep.ok
    assert "MODE_MISMATCH" in {k for k, _ in a.critical}
    assert any(s.api_id == "kt00005" and s.error_class == "MODE_MISMATCH" for s in rep.apis)


def test_guard_blocks_order_api_before_send(sim: Sim, clock: FixedClock) -> None:
    a, g = guarded(sim, clock)
    res = a.place_order(buy())
    assert res.outcome is SubmitOutcome.PRE_SEND_FAILURE  # 전송 전 실패 (결과 불명 아님)
    assert g.blocked == ["kt10000"]
    assert not {api for api, _, _ in sim.log} & ORDER_API_IDS


def test_report_has_no_secrets(sim: Sim, clock: FixedClock, tmp_path: Path) -> None:
    rep, _, _ = run(sim, clock, tmp_path)
    text = json.dumps(rep.to_dict(), ensure_ascii=False)
    for secret in (ACCOUNT_NO, APP_KEY, SECRET, "virtual-token-"):
        assert secret not in text


# --- 진입점: 연결 전 거부 -------------------------------------------------------------------------

def make_root(tmp_path: Path, *, env_lines: list[str], approved: bool | None) -> Path:
    root = tmp_path / "repo"
    (root / "config" / "calendar").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / ".env.paper").write_text("\n".join(env_lines) + "\n", encoding="utf-8")
    (root / "config" / "limits.paper.toml").write_text((Path(__file__).resolve().parents[2] / "config" /
                                                       "limits.paper.toml").read_text(encoding="utf-8"),
                                                      encoding="utf-8")
    app = ['[app]', f'account = "{ALIAS}"', 'loop_seconds = "0.5"', 'reconcile_minutes = 1',
           'sntp_servers = ["sntp.invalid:123"]']
    if approved is not None:
        app += ['[paper]', f'connection_approved = {"true" if approved else "false"}']
    (root / "config" / "app.paper.toml").write_text("\n".join(app) + "\n", encoding="utf-8")
    return root


ENV_OK = ["TRADE_MODE=PAPER", f"KIWOOM_PAPER_APP_KEY={APP_KEY}", f"KIWOOM_PAPER_SECRET_KEY={SECRET}",
          f"KIWOOM_PAPER_ACCOUNT_NO={ACCOUNT_NO}", f"KIWOOM_PAPER_ACCOUNT_ALIAS={ALIAS}"]


@pytest.fixture
def no_network(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    made: list[str] = []
    monkeypatch.setattr("trading_system.app.preflight.KiwoomHttp", lambda *a, **k: made.append("http"))
    return made


@pytest.mark.parametrize("environ, env_lines, approved, reason", [
    ({}, ENV_OK, True, "TRADE_MODE"),
    ({"TRADE_MODE": "BACKTEST"}, ENV_OK, True, "PAPER"),
    ({"TRADE_MODE": "LIVE"}, ENV_OK, True, "LIVE"),
    ({"TRADE_MODE": "PAPER"}, ENV_OK[1:], True, "TRADE_MODE"),
    ({"TRADE_MODE": "PAPER"}, ENV_OK, False, "connection_approved"),
    ({"TRADE_MODE": "PAPER"}, ENV_OK, None, "connection_approved"),
    ({"TRADE_MODE": "PAPER"}, ENV_OK[:-1], True, "KIWOOM_PAPER_ACCOUNT_ALIAS"),
])
def test_entry_refuses_before_any_connection(tmp_path: Path, capsys: pytest.CaptureFixture[str],
                                             no_network: list[str], environ: dict, env_lines: list[str],
                                             approved: bool | None, reason: str) -> None:
    root = make_root(tmp_path, env_lines=env_lines, approved=approved)
    assert preflight_main(root, environ) == 2
    out = capsys.readouterr().out
    assert "연결 없음" in out and reason in out
    assert no_network == []
    for secret in (ACCOUNT_NO, APP_KEY, SECRET):
        assert secret not in out


def test_entry_runs_against_loopback_and_writes_report(tmp_path: Path, sim: Sim, clock: FixedClock,
                                                       capsys: pytest.CaptureFixture[str]) -> None:
    root = make_root(tmp_path, env_lines=ENV_OK, approved=True)
    code = preflight_main(root, {"TRADE_MODE": "PAPER"}, rest_url=sim.url, ws_url=sim.ws_url, clock=clock,
                          probe_code=CODE, per_tr_interval=0.0)
    out = capsys.readouterr().out
    assert code == 0, out
    reports = list((root / "var" / "paper" / "preflight").glob("*.json"))
    assert len(reports) == 1
    text = reports[0].read_text(encoding="utf-8")
    assert json.loads(text)["ok"] is True
    for secret in (ACCOUNT_NO, APP_KEY, SECRET, "virtual-token-"):
        assert secret not in text and secret not in out
    assert not {api for api, _, _ in sim.log} & ORDER_API_IDS


def test_instrument_info_registered_for_probe(tmp_path: Path) -> None:
    # 점검 대상 종목이 배포 설정에 없으면 일반 주식으로 임시 등록해 조회만 한다 (주문 경로 아님)
    s = store()
    from trading_system.app.preflight import probe_info
    info = probe_info({}, CODE)
    s.register(info)
    assert info == InstrumentInfo(CODE, InstrumentKind.STOCK, Decimal("1"), Decimal("1"))
