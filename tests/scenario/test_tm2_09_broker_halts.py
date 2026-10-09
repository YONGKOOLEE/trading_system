"""바이낸스 418·-1021 → 정지 (CP-024 D-113·D-115, 04 V-145(418)·V-146(-1021), T-M2-09).

- 418(IP 차단): 즉시 K5 + 긴급 알림. 자동 재개 없음 — 대조가 일치해도 풀리지 않고, 사람이 차단 해제를 확인(호출 예산 재개 포함)한
  뒤의 대조 일치로만 해제된다(D-113 "사람 재개").
- -1021(타임스탬프): 매번 명시 거부(D-112). 서명 요청(주문·조회)에서 연속 3회면 K6 판정 요청 → K6 발동. 해제는 시계 점검의
  clock_ok 보고(K6 자동 해제 유형 그대로).
네트워크 없음: 합성 전송(tests/contract/test_binance_client.SynthTransport)이 공식 문서 형식 응답을 돌려준다. 값은 모두 가상이다.
"""

from __future__ import annotations

from collections.abc import Iterator
from decimal import Decimal
from pathlib import Path

import pytest

from tests.contract.test_binance_client import ACK, STEPS, Env, _order, _reply
from tests.scenario.test_tm2_09_v157 import run, sig, spot_reconciler
from tests.support.invariants import check_invariants
from tests.support.m2_fakes import ACCOUNT, M2Env, build_m2_env
from trading_system.broker.binance import client as c
from trading_system.broker.binance.classify import QueryKind
from trading_system.broker.binance.weights import CallPriority as P
from trading_system.domain.enums import Check, GateResult, HaltType, Mode, RequestType
from trading_system.domain.enums import SubmitOutcome as O
from trading_system.halt.broker_alerts import BrokerAlertHalts
from trading_system.halt.manager import K5_LINK_IP_BAN, Channel
from trading_system.monitoring.sntp import ClockMonitor
from trading_system.reconciliation.spot import PERIODIC
from trading_system.storage.alerts import AuditAlertSink

CLOCK_ERR = {"code": -1021, "msg": "Timestamp for this request is outside of the recvWindow."}


@pytest.fixture
def m2(tmp_path: Path) -> Iterator[M2Env]:
    e = build_m2_env(tmp_path)
    yield e
    violations = check_invariants(e.repo.connection, Mode.BACKTEST.value)
    e.repo.close()
    assert violations == [], violations


def wired(m2: M2Env, replies: list[object]) -> tuple[Env, BrokerAlertHalts]:
    """합성 전송 클라이언트의 경보 → 정지 연결 (어댑터 on_alert 자리에 BrokerAlertHalts)."""
    cli = Env(replies)
    bridge = BrokerAlertHalts(m2.halts, m2.repo, m2.clock, AuditAlertSink(m2.repo, m2.clock, actor="binance"),
                              on_ip_ban_cleared=cli.budget.resume_after_ban)
    cli.client._alert = bridge  # noqa: SLF001 — 조립 지점(T-M2-14)이 생성자 on_alert로 넣을 자리
    return cli, bridge


def halts(m2: M2Env) -> list[str]:
    return m2.repo.active_halts(ACCOUNT)


# --- 클라이언트: -1021 연속 판정 (서명 요청 전부) ------------------------------------------------------------

def test_minus_1021_on_signed_queries_counts_toward_k6() -> None:
    env = Env([_reply(400, CLOCK_ERR), _reply(400, CLOCK_ERR), _reply(400, CLOCK_ERR)])
    assert env.client.place(_order(), P.INCREASE, **STEPS).outcome is O.REJECTED
    for _ in range(2):
        with pytest.raises(c.BinanceQueryError) as e:
            env.client.query_order("BTCUSDT", order_id=28)
        assert e.value.kind is QueryKind.CLOCK
    assert [k for k, _ in env.alerts].count("K6") == 1
    assert len(env.transport.sent) == 3          # 시각 오류 조회는 재시도하지 않는다


def test_minus_1021_streak_reset_by_signed_query_success() -> None:
    env = Env([_reply(400, CLOCK_ERR), _reply(400, CLOCK_ERR), _reply(200, ACK), _reply(400, CLOCK_ERR)])
    env.client.place(_order(), P.INCREASE, **STEPS)
    env.client.place(_order(), P.INCREASE, **STEPS)
    assert env.client.query_order("BTCUSDT", order_id=28).kind is QueryKind.OK
    env.client.place(_order(), P.INCREASE, **STEPS)
    assert "K6" not in [k for k, _ in env.alerts]


def test_first_minus_1021_raises_clock_warning() -> None:
    env = Env([_reply(400, CLOCK_ERR), _reply(400, CLOCK_ERR)])
    env.client.place(_order(), P.INCREASE, **STEPS)
    env.client.place(_order(), P.INCREASE, **STEPS)
    assert [k for k, _ in env.alerts] == ["CLOCK"]          # 연속 구간 시작에 시계 경고 1회 (D-115)


# --- 418 → 즉시 K5, 사람 재개 ---------------------------------------------------------------------------

def test_418_raises_k5_immediately_and_blocks_risk_increase(m2: M2Env) -> None:
    cli, _ = wired(m2, [_reply(418, {"code": -1003, "msg": "IP banned"})])
    assert cli.client.place(_order(), P.REDUCE, **STEPS).outcome is O.UNKNOWN
    assert "K5" in halts(m2)
    assert run(m2, sig(RequestType.ENTRY, env=m2)).failed_check is Check.G2
    [row] = [r for r in m2.repo.halt_rows(None) if r["halt_type"] == "K5"]
    assert row["account"] is None                             # IP 차단 = 이 프로그램의 모든 호출
    alerts = [e for e in m2.repo.audit_events() if e["kind"] == "ALERT"]
    assert any('"EMERGENCY"' in e["summary"] and "K5" in e["summary"] for e in alerts)


def test_418_k5_not_released_by_reconcile_alone(m2: M2Env) -> None:
    cli, _ = wired(m2, [_reply(418, {"code": -1003, "msg": "IP banned"})])
    cli.client.place(_order(), P.REDUCE, **STEPS)
    assert spot_reconciler(m2).run(ACCOUNT, PERIODIC).result == "MATCH"
    assert "K5" in halts(m2)                                   # 자동 재개 없음 (D-113)
    m2.halts.report_condition(HaltType.K5, None, "broker_connected", True, "경로 구분 없는 연결 보고")
    assert spot_reconciler(m2).run(ACCOUNT, PERIODIC).result == "MATCH"
    assert "K5" in halts(m2)                                   # 일반 연결 보고로도 차단 경로는 풀리지 않는다
    r = m2.halts.resume(HaltType.K5, None, Channel.LOCAL, "user")
    assert not r.ok and K5_LINK_IP_BAN in r.reason
    assert cli.budget.banned


def test_418_released_after_human_confirm_then_reconcile(m2: M2Env) -> None:
    cli, bridge = wired(m2, [_reply(418, {"code": -1003, "msg": "IP banned"}), _reply(200, ACK)])
    cli.client.place(_order(), P.REDUCE, **STEPS)
    bridge.confirm_ip_ban_cleared("user")
    assert not cli.budget.banned                               # 호출 예산 재개는 사람 확인으로만
    assert "K5" in halts(m2)                                   # 확인 뒤에도 대조 전에는 유지
    assert spot_reconciler(m2).run(ACCOUNT, PERIODIC).result == "MATCH"
    assert "K5" not in halts(m2)
    assert cli.client.place(_order(), P.REDUCE, **STEPS).outcome is O.ACCEPTED
    kinds = [e["kind"] for e in m2.repo.audit_events()]
    assert "IP_BAN_CLEARED" in kinds


def test_confirm_without_ban_is_noop_record(m2: M2Env) -> None:
    _, bridge = wired(m2, [])
    bridge.confirm_ip_ban_cleared("user")
    assert halts(m2) == []


# --- -1021 연속 3회 → K6 판정 요청 → 시계 점검으로 해제 ----------------------------------------------------------

def test_minus_1021_three_in_a_row_raises_k6_and_clock_check_releases(m2: M2Env) -> None:
    cli, _ = wired(m2, [_reply(400, CLOCK_ERR)] * 3)
    for _ in range(3):
        assert cli.client.place(_order(), P.INCREASE, **STEPS).outcome is O.REJECTED
    assert "K6" in halts(m2)
    assert run(m2, sig(RequestType.ENTRY, env=m2)).failed_check is Check.G2
    ClockMonitor(m2.repo, m2.clock, [("a", 1)], m2.halts, None, lambda h, p: Decimal("0.1"), samples=1).check()
    assert "K6" not in halts(m2)                               # 시계 판정 정상 → 자동 해제(K6 유형 그대로)
    assert run(m2, sig(RequestType.ENTRY, ev="e2", env=m2)).result is GateResult.APPROVED


def test_two_minus_1021_only_warn_no_halt(m2: M2Env) -> None:
    cli, _ = wired(m2, [_reply(400, CLOCK_ERR)] * 2)
    for _ in range(2):
        cli.client.place(_order(), P.INCREASE, **STEPS)
    assert halts(m2) == []
    assert any("-1021" in e["summary"] for e in m2.repo.audit_events() if e["kind"] == "ALERT")
