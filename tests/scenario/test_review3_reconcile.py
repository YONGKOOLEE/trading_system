"""3차 검토 결함 — 대조·재시작 복구 (CLAUDE.md §8-6·8-9, 01 §2.11·K8, 03 D-064·§6.3·§6.4).

1. 실행 중 K8(루프 지연·시계 점프·엔진 단계 오류)은 대조 뒤 복구 조건을 다시 판정해 사용자 재개가 가능해야 한다.
   K8은 자동으로 풀리지 않는다(사용자 재개). 복구 판정은 tick마다가 아니라 대조마다 한 번.
2. 응답 유실 주문(의도 결과 불명)을 주기 대조가 '미확인 브로커 주문'(K4)으로 잡지 않는다 — 결과 불명 해소(K3 경로)에
   맡긴다. 해소기가 채택한 뒤에는 알려진 주문이며, 그전에 열린 항목은 다음 대조에서 자동 종결한다.
   수량이 다른 브로커 주문은 여전히 미확인 주문(보수적).
"""

from __future__ import annotations

import json
from datetime import timedelta
from decimal import Decimal
from pathlib import Path

import pytest

from tests.scenario.conftest import ACCOUNT, env  # noqa: F401
from tests.scenario.test_t005_halts import _limit_buy
from tests.scenario.test_t020_app import ACCOUNT as APP_ACCOUNT
from tests.scenario.test_t020_app import World
from trading_system.app.supervisor import QueuedHalts
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.execution.resolver import UnknownResolver
from trading_system.monitoring.liveness import LoopMonitor
from trading_system.reconciliation.reconciler import ItemKind, Reconciler
from trading_system.storage.alerts import AuditAlertSink


# --- 1. 실행 중 K8 → 대조 → 사용자 재개 ---------------------------------------------------------------

@pytest.fixture
def world(tmp_path: Path):  # type: ignore[no-untyped-def]
    w = World(tmp_path)
    yield w
    w.close()


def _k8_condition_audits(w: World) -> list[dict]:
    out = []
    for e in w.repo.audit_events():
        if e["kind"] == "HALT_CONDITION":
            s = json.loads(e["summary"])
            if s.get("type") == "K8" and s.get("condition") == "recovery_ok":
                out.append(s)
    return out


def _last_command(w: World) -> dict:
    return w.repo.select("SELECT command, status, result FROM ops_command ORDER BY created_seq")[-1]


def _clock_jump_k8(w: World) -> None:
    lm = LoopMonitor(QueuedHalts(w.engine), w.engine.alerts)
    t0 = w.clock.now()
    lm.tick(100.0, t0, None)
    lm.tick(101.0, t0 + timedelta(seconds=40), 1.0)   # 벽시각만 40초 점프 (절전 해제 의심, D-064)
    w.engine.tick()                                     # 판정·실행 스레드가 K8 발동
    assert "K8" in w.repo.active_halts(APP_ACCOUNT)


def test_runtime_k8_clock_jump_resumable_after_reconcile_not_auto_released(world: World) -> None:
    world.engine.start()
    world.resume_k8()
    assert "K8" not in world.repo.active_halts(APP_ACCOUNT)
    _clock_jump_k8(world)
    world.resume_k8()                                   # 대조 전: 재개 거부
    assert _last_command(world)["status"] == "REJECTED"
    world.at(9, 1)
    world.engine.tick()                                 # 장중 주기 대조 → 복구 판정
    assert "K8" in world.repo.active_halts(APP_ACCOUNT)  # 자동 해제 없음 (사용자 재개)
    world.resume_k8()
    assert _last_command(world)["status"] == "DONE", _last_command(world)
    assert "K8" not in world.repo.active_halts(APP_ACCOUNT)


def test_runtime_k8_engine_step_error_resumable_after_reconcile(world: World, monkeypatch: pytest.MonkeyPatch) -> None:
    world.engine.start()
    world.resume_k8()

    def boom(now):  # type: ignore[no-untyped-def]
        raise RuntimeError("시험 단계 오류")

    monkeypatch.setattr(world.engine, "_exec_policy", boom)
    world.engine.tick()
    monkeypatch.undo()
    assert "K8" in world.repo.active_halts(APP_ACCOUNT)
    world.at(9, 1)
    world.engine.tick()
    world.resume_k8()
    assert _last_command(world)["status"] == "DONE", _last_command(world)
    assert "K8" not in world.repo.active_halts(APP_ACCOUNT)


def test_runtime_k8_recovery_check_once_per_reconcile_not_every_tick(world: World) -> None:
    world.engine.start()
    world.resume_k8()
    _clock_jump_k8(world)
    n = len(_k8_condition_audits(world))
    for s in (10, 20, 30):                              # 장 시작 전: 주기 대조 없음 → 복구 판정 없음
        world.at(8, 59, s)
        world.engine.tick()
    assert len(_k8_condition_audits(world)) == n
    world.at(9, 1)
    world.engine.tick()                                 # 대조 1회 → 복구 판정 1회
    world.engine.tick()                                 # 같은 분 다음 tick: 대조 없음 → 판정 없음
    after = _k8_condition_audits(world)[n:]
    assert len(after) == 1 and after[0]["ok"] is True, after


def test_runtime_k8_not_resumable_when_reconcile_mismatch(world: World) -> None:
    world.engine.start()
    world.resume_k8()
    _clock_jump_k8(world)
    world.broker.add_external_order(APP_ACCOUNT, "TST-A", "BUY", Decimal("3"))   # 절전 중 외부 주문 → 대조 불일치
    world.at(9, 1)
    world.engine.tick()
    world.resume_k8()
    assert _last_command(world)["status"] == "REJECTED"
    assert "K8" in world.repo.active_halts(APP_ACCOUNT) and "K4" in world.repo.active_halts(APP_ACCOUNT)
    assert _k8_condition_audits(world)[-1]["ok"] is False


def test_runtime_k8_requested_reconcile_outside_session_reports_recovery(world: World) -> None:
    """장 밖(08:59)에도 요청 대조(request_reconcile — 재접속 등)가 복구 판정을 한다."""
    world.engine.start()
    world.resume_k8()
    _clock_jump_k8(world)
    world.engine.request_reconcile()
    world.engine.tick()
    world.resume_k8()
    assert _last_command(world)["status"] == "DONE", _last_command(world)


# --- 2. 결과 불명 주문 vs 주기 대조 ----------------------------------------------------------------

def _rec(env):  # type: ignore[no-untyped-def]
    return Reconciler(env.repo, env.broker, env.clock, env.halts, AuditAlertSink(env.repo, env.clock),
                      drain_events=env.executor.process_events)


def _unknown_buy(env, inst: str = "TST-B"):  # type: ignore[no-untyped-def]
    env.broker.behavior = FakeBehavior.LOST_ACCEPT
    _limit_buy(env, inst, f"ev-{inst}")
    env.broker.behavior = FakeBehavior.FILL
    [row] = [r for r in env.repo.select("SELECT intent_id, state FROM intent") if r["state"] == "UNKNOWN"]
    return row["intent_id"]


def test_unknown_intent_order_not_flagged_by_periodic_reconcile_then_adopted(env) -> None:  # noqa: F811
    iid = _unknown_buy(env)
    rec = _rec(env)
    r = rec.run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH", env.repo.select("SELECT kind, ref FROM recon_item")
    assert "K4" not in env.repo.active_halts(ACCOUNT)
    assert env.repo.open_recon_items(ACCOUNT) == []
    res = UnknownResolver(env.repo, env.broker, env.executor, env.clock, wait=lambda s: None)
    assert res.resolve(iid).name == "FOUND"
    r2 = rec.run(ACCOUNT, "PERIODIC")
    assert r2.result == "MATCH" and "K4" not in env.repo.active_halts(ACCOUNT)


def test_different_quantity_broker_order_still_flagged_while_unknown(env) -> None:  # noqa: F811
    """보수적: 결과 불명 의도와 수량이 다른 브로커 주문은 미확인 주문(K4)."""
    _unknown_buy(env)
    env.broker.add_external_order(ACCOUNT, "TST-B", "BUY", Decimal("7"))
    r = _rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MISMATCH"
    kinds = [(i["kind"], i["broker_value"]) for i in env.repo.open_recon_items(ACCOUNT)]
    assert len(kinds) == 1 and kinds[0][0] == ItemKind.UNKNOWN_BROKER_ORDER and kinds[0][1].startswith("BUY 7"), kinds
    assert "K4" in env.repo.active_halts(ACCOUNT)


def test_two_candidate_orders_same_terms_not_k4_left_to_resolver(env) -> None:  # noqa: F811
    """후보가 둘 이상이어도 K4가 아니라 해소기의 모호 판정(K3)에 맡긴다."""
    _unknown_buy(env, "TST-B")
    env.broker.add_external_order(ACCOUNT, "TST-B", "BUY", Decimal("50"))
    r = _rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH" and "K4" not in env.repo.active_halts(ACCOUNT)


def test_open_unknown_order_item_auto_resolved_after_adoption(env) -> None:  # noqa: F811
    """이전 대조가 이미 '미확인 브로커 주문' 항목을 열어 두었다면(수정 전 기록 등), 해소기가 그 주문을 채택한 뒤의
    대조에서 항목을 자동 종결한다 (원장은 바꾸지 않음 — 항목만 종결, 감사 기록)."""
    iid = _unknown_buy(env)
    [o] = [o for o in env.broker.query_orders_unchecked() if o.kind == "NEW"]
    now = env.clock.now()
    today = env.repo.trade_date_of(now)
    with env.repo.tx():
        rid = env.repo.insert_reconciliation(ACCOUNT, "PERIODIC", "MISMATCH", {}, now)
        item = env.repo.insert_recon_item(rid, ACCOUNT, ItemKind.UNKNOWN_BROKER_ORDER, o.instrument, o.order_no, None,
                                          f"{o.side} {o.quantity} {o.state}",
                                          json.dumps({"side": o.side, "quantity": str(o.quantity),
                                                      "trade_date": today, "detected_at": now.isoformat()}))
    rec = _rec(env)
    assert rec.run(ACCOUNT, "PERIODIC").result == "MISMATCH"     # 채택 전: 항목 유지
    UnknownResolver(env.repo, env.broker, env.executor, env.clock, wait=lambda s: None).resolve(iid)
    r = rec.run(ACCOUNT, "PERIODIC")
    assert r.result == "MATCH", env.repo.open_recon_items(ACCOUNT)
    row = env.repo.recon_item(item)
    assert row["state"] == "RESOLVED" and row["resolver"] == "reconciler", row


def test_external_resolve_still_refused_for_flagged_order_while_unknown(env) -> None:  # noqa: F811
    """test_t008의 '결과 불명 중 EXTERNAL 해소 거부' 보호는 계속 유효 — 조건이 다른(수량) 브로커 주문은 여전히
    항목이 되고, 같은 종목·방향의 결과 불명이 있으면 EXTERNAL로 해소할 수 없다."""
    from trading_system.ops.reconcile import ReconcileResolver, ResolveError
    _unknown_buy(env, "TST-B")
    env.broker.add_external_order(ACCOUNT, "TST-B", "BUY", Decimal("3"))
    _rec(env).run(ACCOUNT, "PERIODIC")
    [it] = env.repo.open_recon_items(ACCOUNT)
    with pytest.raises(ResolveError, match="결과 불명"):
        ReconcileResolver(env.repo, env.executor, env.clock).resolve(it["item_id"], "EXTERNAL", "u")


# --- 3. 장 마감 후 대조 (03 §6.4) — 단위 수준 보수 조건 --------------------------------------------------

def _partial_open_buy(env):  # type: ignore[no-untyped-def]
    env.broker.behavior = FakeBehavior.ACCEPT_ONLY
    _limit_buy(env, "TST-C", "ev-close")
    env.broker.behavior = FakeBehavior.FILL
    [no] = env.broker.open_order_nos()
    env.broker.fill(no, Decimal("10"))
    env.executor.process_events()
    [it] = env.repo.select("SELECT intent_id, state FROM intent")
    assert it["state"] == "PARTIAL"
    return no, it["intent_id"]


def _closing_rec(env):  # type: ignore[no-untyped-def]
    rec = _rec(env)

    def expire(order, trade_date):  # type: ignore[no-untyped-def]
        from trading_system.domain.models import OrderEvent
        with env.repo.tx():
            env.executor.apply_event(OrderEvent(kind="EXPIRED", order_no=order.order_no, account=order.account,
                                                trade_date=trade_date))
    rec.expire_order = expire
    return rec


def test_close_reconcile_expires_silently_expired_remainder_and_releases_reservation(env) -> None:  # noqa: F811
    no, iid = _partial_open_buy(env)
    env.broker._set(no, state="EXPIRED")  # noqa: SLF001 — 통보 없는 마감 소멸
    r = _closing_rec(env).run(ACCOUNT, "CLOSE")
    assert r.result == "MATCH", env.repo.open_recon_items(ACCOUNT)
    assert env.repo.intent(iid).state.value == "EXPIRED"
    assert env.repo.active_reservations(ACCOUNT) == []
    assert "K4" not in env.repo.active_halts(ACCOUNT)


def test_periodic_reconcile_does_not_expire_only_close_does(env) -> None:  # noqa: F811
    """만료 종결은 마감 후 대조에서만. 장중에 브로커만 종결이면 종전대로 상태 차이 → K4."""
    no, iid = _partial_open_buy(env)
    env.broker._set(no, state="EXPIRED")  # noqa: SLF001
    r = _closing_rec(env).run(ACCOUNT, "PERIODIC")
    assert r.result == "MISMATCH"
    assert [i["kind"] for i in env.repo.open_recon_items(ACCOUNT)] == [ItemKind.STATE_MISMATCH]
    assert env.repo.intent(iid).state.value == "PARTIAL"


def test_close_reconcile_filled_quantity_differs_not_expired(env) -> None:  # noqa: F811
    """브로커 체결 수량이 내부와 다르면 만료 종결하지 않는다 (보수적 — 사람 확인)."""
    no, iid = _partial_open_buy(env)
    env.broker._set(no, state="EXPIRED", filled_quantity=Decimal("20"))  # noqa: SLF001
    r = _closing_rec(env).run(ACCOUNT, "CLOSE")
    assert r.result == "MISMATCH"
    assert ItemKind.STATE_MISMATCH in [i["kind"] for i in env.repo.open_recon_items(ACCOUNT)]
    assert env.repo.intent(iid).state.value == "PARTIAL" and "K4" in env.repo.active_halts(ACCOUNT)


def test_close_reconcile_without_expire_path_keeps_state_mismatch(env) -> None:  # noqa: F811
    """만료 경로(실행기)가 연결되지 않은 대조기(예: preflight)는 종전대로 상태 차이."""
    no, iid = _partial_open_buy(env)
    env.broker._set(no, state="EXPIRED")  # noqa: SLF001
    r = _rec(env).run(ACCOUNT, "CLOSE")
    assert r.result == "MISMATCH" and env.repo.intent(iid).state.value == "PARTIAL"


def test_close_reconcile_open_remainder_alerts_warning(env) -> None:  # noqa: F811
    _partial_open_buy(env)
    _closing_rec(env).run(ACCOUNT, "CLOSE")
    msgs = [json.loads(e["summary"]) for e in env.repo.audit_events() if e["kind"] == "ALERT"]
    hits = [m for m in msgs if "장 마감 후 미체결" in m.get("message", "")]
    assert len(hits) == 1 and hits[0]["level"] == "WARNING", msgs
