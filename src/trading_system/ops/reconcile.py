"""대조 불일치 해소 (02 ENT-24a·D-034·D-035, 03 §6.3, `scripts/reconcile-resolve`). 로컬 운영 명령으로만.

사람이 항목마다 해소 방법을 고른다. 시스템이 스스로 고르지 않는다.
- EXTERNAL: 외부 거래로 반영. 수량 차이는 내부 보유를 브로커 값으로 맞추고, 새로 생긴 보유는 소유자 "수동"
  (전략 신호 불가, 운영 요청만, INV-16). 전략 소유 종목에 외부 매수가 섞이면 거부(BROKER_ACTION으로 정리).
- APPLY_FILL: 누락 체결 반영. 조회한 체결(체결번호 있음)을 통보와 같은 경로로 반영한다.
- CLOSE_ORDER: 내부에만 열린 주문·상태 차이·지난 거래일 주문을 브로커 상태(기본: 만료)로 종결한다.
- BROKER_ACTION: 사람이 증권사에서 조치했음. 내부는 바꾸지 않고 다음 재대조로 확인한다.
해소 후 재대조가 일치해야 K4 재개 조건이 충족된다.
"""

from __future__ import annotations

import json
from decimal import Decimal

from trading_system.domain.clock import Clock, from_iso
from trading_system.domain.models import MANUAL_OWNER, OrderEvent
from trading_system.execution.executor import Executor
from trading_system.storage.repository import Repository

METHODS = {
    "QTY_MISMATCH": {"EXTERNAL", "BROKER_ACTION"},
    "UNKNOWN_BROKER_ORDER": {"EXTERNAL", "BROKER_ACTION"},
    "MISSING_FILL": {"APPLY_FILL", "BROKER_ACTION"},
    "EXCESS_FILL": {"BROKER_ACTION"},
    "INTERNAL_ONLY_ORDER": {"CLOSE_ORDER", "BROKER_ACTION"},
    "STATE_MISMATCH": {"CLOSE_ORDER", "BROKER_ACTION"},
    "STALE_ORDER": {"CLOSE_ORDER", "BROKER_ACTION"},
}


class ResolveError(ValueError):
    pass


_NOT_APPLIED = "NOT_APPLIED"


class ReconcileResolver:
    def __init__(self, repo: Repository, executor: Executor, clock: Clock) -> None:
        self._repo = repo
        self._executor = executor
        self._clock = clock

    def resolve(self, item_id: str, method: str, actor: str) -> str:
        item = self._repo.recon_item(item_id)
        if item is None or item["state"] != "OPEN":
            raise ResolveError("열린 불일치 항목이 아님")
        if method not in METHODS.get(item["kind"], set()):
            raise ResolveError(f"{item['kind']}에는 {sorted(METHODS.get(item['kind'], set()))}만 가능")
        detail = json.loads(item["detail"] or "{}")
        with self._repo.tx():
            now = self._clock.now()
            note = self._apply(item, method, detail)
            if note is _NOT_APPLIED:
                self._repo.audit(now, "RECONCILE_RESOLVE_FAILED", actor, item_id,
                                 {"kind": item["kind"], "method": method})
            else:
                self._repo.resolve_recon_item(item_id, method, actor, now)
                self._repo.audit(now, "RECONCILE_RESOLVE", actor, item_id,
                                 {"kind": item["kind"], "method": method, "note": note})
        if note is _NOT_APPLIED:
            raise ResolveError("체결이 반영되지 않음(이상 통보로 거부) — 감사 기록 확인 후 BROKER_ACTION")
        return note

    def _apply(self, item: dict, method: str, detail: dict) -> str:
        account, inst = item["account"], item["instrument"]
        if method == "BROKER_ACTION":
            return "사람이 증권사에서 조치 — 재대조로 확인"
        if item["kind"] == "QTY_MISMATCH":
            internal, broker = Decimal(item["internal_value"]), Decimal(item["broker_value"])
            if any(i["kind"] == "MISSING_FILL" and i["instrument"] == inst
                   for i in self._repo.open_recon_items(account)):
                raise ResolveError("같은 종목에 누락 체결 항목이 열려 있음 — APPLY_FILL로 먼저 반영 후 재대조")
            if self._repo.position(account, inst).quantity != internal:
                # 대조 뒤에 체결 등으로 원장이 바뀌었다. 브로커 값으로 덮으면 그 사이 체결이 사라진다
                raise ResolveError("대조 이후 내부 보유가 바뀜 — 재대조 후 새 항목으로 해소 (이 항목은 BROKER_ACTION)")
            own = self._repo.active_ownership(account, inst)
            if broker > internal and own is not None and own.owner != MANUAL_OWNER:
                raise ResolveError("전략 소유 종목에 외부 매수가 섞임 — 증권사에서 정리 후 BROKER_ACTION")
            pos = self._repo.position(account, inst)
            self._repo.set_position(account, inst, broker, pos.avg_price if broker > 0 else Decimal("0"))
            if broker > 0 and own is None:
                self._repo.insert_ownership(account, inst, MANUAL_OWNER, None, self._clock.now())
                new_own = self._repo.active_ownership(account, inst)
                assert new_own is not None
                self._repo.set_ownership_state(new_own.ownership_id, "CONFIRMED", None)
            elif broker == 0 and own is not None:
                self._repo.set_ownership_state(own.ownership_id, "RELEASED", "외부 거래로 보유 0", self._clock.now())
            return f"내부 보유 {internal} → {broker} (외부 거래 반영, 평균단가 미상이면 유지)"
        if item["kind"] == "UNKNOWN_BROKER_ORDER":
            side = detail.get("side")
            if any(u.side == side for u in self._repo.unknown_intents(account, inst)):
                raise ResolveError("같은 종목·방향에 결과 불명 의도가 있음 — 우리 주문일 수 있으니 결과 불명부터 확정 (K3)")
            # 외부 주문으로 기록해 다음 대조에서 다시 "미확인"으로 잡히지 않게 한다. 의도·포지션은 바꾸지 않는다
            # (체결분은 수량 대조 항목으로 따로 해소)
            # 발견한 거래일로 기록한다. 해소일로 적으면 다음 날 같은 주문번호의 우리 주문과 충돌한다 (D-046)
            at = from_iso(detail["detected_at"]) if detail.get("detected_at") else self._clock.now()
            if self._repo.broker_order_by_no(item["ref"], self._repo.trade_date_of(at), account) is not None:
                return "이미 기록된 주문 — 원장 변경 없음"
            self._repo.insert_broker_order(
                account=account, order_no=item["ref"], at=at, intent_id=None, instrument=inst,
                side=detail.get("side", "BUY"), quantity=Decimal(detail.get("quantity", "0")), origin="EXTERNAL")
            return "외부 주문으로 인정 — 원장 변경 없음(체결분은 수량 대조로 반영)"
        if item["kind"] == "MISSING_FILL":
            order = self._repo.broker_order(detail["order_key"])
            if order is None:
                raise ResolveError("대상 주문 없음")
            # T-017 재검토 2: 항목을 만든 뒤 내부 체결이 바뀌었거나, 같은 주문에 다른 누락 항목이 열려 있으면
            # 차이 수량이 낡아 이중 반영될 수 있다 → 재대조로 새 항목을 만들게 한다
            if "internal_total" in detail and order.filled_quantity != Decimal(detail["internal_total"]):
                raise ResolveError("대조 이후 내부 체결이 바뀜 — 재대조 후 새 항목으로 해소 (이 항목은 BROKER_ACTION)")
            others = [i for i in self._repo.open_recon_items(account) if i["kind"] == "MISSING_FILL"
                      and i["item_id"] != item["item_id"]
                      and json.loads(i["detail"] or "{}").get("order_key") == detail["order_key"]]
            if others:
                raise ResolveError("같은 주문에 누락 체결 항목이 여럿 — 최신 항목만 반영하고 나머지는 BROKER_ACTION")
            if not detail.get("price"):
                raise ResolveError("조회 체결 행이 없어 체결가를 모름 — 증권사 확인 후 BROKER_ACTION")
            self._executor.apply_event(OrderEvent(
                kind="FILL", order_no=order.order_no, fill_id=detail["fill_id"],
                quantity=Decimal(detail["quantity"]), price=Decimal(detail["price"]), at=from_iso(detail["at"]),
                account=account, trade_date=detail["order_key"].split("|")[1]))
            if not self._repo.fill_exists(order.order_key, detail["fill_id"]):
                return _NOT_APPLIED  # 이상 통보로 거부됨. 이상 기록(감사·K4)은 남기고 항목은 열어 둔다
            return "누락 체결 반영"
        order = self._repo.broker_order(detail["order_key"])
        if order is None or order.state not in ("ACCEPTED", "PARTIAL"):
            return "이미 종결됨"
        kind = detail.get("broker_state") if detail.get("broker_state") in ("CANCELLED", "EXPIRED", "REJECTED") \
            else "EXPIRED"
        self._executor.apply_event(OrderEvent(kind=kind, order_no=order.order_no, account=account,
                                              trade_date=detail["order_key"].split("|")[1]))
        return f"내부 주문 종결({kind})"
