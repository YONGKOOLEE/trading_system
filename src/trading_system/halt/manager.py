"""정지 관리 (R-07, 01 §2.6). T-001 범위: K1 수동 킬 스위치 발동.

- 정지는 청산이 아니다. 보유 포지션을 자동으로 팔지 않는다 (CLAUDE.md §8-8).
- K1: 신규 진입·증액 차단(게이트 G2), 미체결 매수는 취소 요청을 만들어 **게이트를 거쳐** 보낸다.
- 재개 조건·유형별 세부 동작·원격 명령은 T-005·T-013.
"""

from __future__ import annotations

from trading_system.domain.clock import Clock
from trading_system.domain.enums import GateResult, HaltType, RequestSource, Side
from trading_system.domain.models import GateRequest
from trading_system.execution.executor import Executor
from trading_system.gate.gate import Gate
from trading_system.storage.repository import Repository


class HaltManager:
    def __init__(self, repo: Repository, gate: Gate, executor: Executor, clock: Clock) -> None:
        self._repo = repo
        self._gate = gate
        self._executor = executor
        self._clock = clock

    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None) -> str:
        with self._repo.tx():
            now = self._clock.now()
            halt_id = self._repo.insert_halt(halt_type.value, account, actor, reason, now)
            self._repo.audit(now, "HALT_ACTIVATED", actor, halt_id, {"type": halt_type.value, "account": account})
        if halt_type is HaltType.K1:
            self._cancel_open_buys(halt_id, account)
        return halt_id

    def _cancel_open_buys(self, halt_id: str, account: str | None) -> None:
        accounts = [account] if account else self._repo.accounts()
        for acct in accounts:
            for order in self._repo.open_orders(acct, Side.BUY.value):
                owner = self._repo.intent(order.intent_id).owner if order.intent_id else None
                req = GateRequest.cancel(
                    RequestSource.HALT_POLICY, acct, order.instrument, Side.BUY, order.order_key,
                    idem_key=f"halt:{halt_id}:{order.order_key}", owner=owner,
                )
                decision = self._gate.decide(req)
                if decision.result is GateResult.APPROVED and decision.approved is not None:
                    self._executor.submit(decision.approved)
