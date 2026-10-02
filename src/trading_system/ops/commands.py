"""운영 명령 처리 (R-07, R-21, 01 §2.9, 03 §6.6).

로컬 CLI·메신저(T-013) → DB 명령 테이블(명령 ID = 멱등키) → 이 처리기 → 정지 관리 또는 게이트.
- 운영 요청은 취소·감액·청산만 (신규 진입·증액 불가, R-21). 요청은 반드시 게이트를 거친다.
- 원격 채널: 킬 스위치 발동·K1/K2 재개·취소·감액·청산. 결과 불명 확정은 로컬만.
- 원격 인증(허용 채팅 ID·확인 코드)은 T-013. 이 처리기는 기록된 채널 값을 믿는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation

from trading_system.domain.clock import Clock
from trading_system.domain.enums import GateResult, HaltType, OrderType, RequestSource, RequestType, Side
from trading_system.domain.models import GateRequest
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.gate.gate import Gate
from trading_system.halt.manager import Channel, HaltManager
from trading_system.storage.repository import Repository

LOCAL_ONLY = frozenset({"CONFIRM_UNKNOWN"})
KNOWN = frozenset({"KILL", "RESUME", "CANCEL", "REDUCE", "EXIT", "CONFIRM_UNKNOWN"})


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class CommandOutcome:
    command_id: str
    status: str  # DONE / REJECTED / FAILED
    result: str


class OpsProcessor:
    def __init__(self, repo: Repository, gate: Gate, executor: Executor, halts: HaltManager, clock: Clock) -> None:
        self._repo = repo
        self._gate = gate
        self._executor = executor
        self._halts = halts
        self._clock = clock

    def process_pending(self) -> list[CommandOutcome]:
        out = []
        for cmd in self._repo.pending_commands():
            out.append(self._process(cmd))
        return out

    def _process(self, cmd: dict) -> CommandOutcome:
        cid, channel, actor, name = cmd["command_id"], cmd["channel"], cmd["actor"], cmd["command"]
        try:
            args = json.loads(cmd["args"])
            if name not in KNOWN:
                raise CommandError(f"알 수 없는 명령 {name}")
            if channel not in (Channel.LOCAL, Channel.REMOTE):
                raise CommandError(f"알 수 없는 채널 {channel}")
            if name in LOCAL_ONLY and channel != Channel.LOCAL:
                raise CommandError(f"{name}은 로컬에서만 가능")
            status, result = self._dispatch(cid, channel, actor, name, args)
        except (CommandError, ValueError, KeyError, InvalidOperation) as exc:
            status, result = "REJECTED", f"{type(exc).__name__}: {exc}"
        except ExecutionBlocked as exc:
            status, result = "FAILED", f"실행기 차단: {exc}"
        with self._repo.tx():
            now = self._clock.now()
            self._repo.finish_command(cid, status, result, now)
            self._repo.audit(now, "OPS_COMMAND", actor, cid,
                             {"channel": channel, "command": name, "status": status, "result": result})
        return CommandOutcome(cid, status, result)

    def _dispatch(self, cid: str, channel: str, actor: str, name: str, args: dict) -> tuple[str, str]:
        account = args.get("account")
        if name == "KILL":
            halt_id = self._halts.activate(HaltType.K1, f"{channel}:{actor}", args.get("reason") or "운영자 킬 스위치",
                                           account)
            return "DONE", f"K1 발동 {halt_id}"
        if name == "RESUME":
            r = self._halts.resume(HaltType(args["halt_type"]), account, channel, actor)
            return ("DONE" if r.ok else "REJECTED"), r.reason
        if name == "CONFIRM_UNKNOWN":
            self._halts.confirm_unknown(args["intent_id"], bool(args["accepted"]), actor, args.get("order_no"))
            return "DONE", "결과 불명 확정"
        req = self._request(cid, name, args)
        d = self._gate.decide(req)
        if d.result is not GateResult.APPROVED or d.approved is None:
            self._executor.alert("EMERGENCY", f"운영 요청 거부 ({name}): {d.reason}", cid)  # 위험 축소 거부는 알림
            return "REJECTED", f"게이트 거부 {d.failed_check}: {d.reason}"
        state = self._executor.submit(d.approved)
        return "DONE", f"의도 {d.approved.intent_id} {state.value}"

    def _request(self, cid: str, name: str, args: dict) -> GateRequest:
        idem = f"ops:{cid}"
        if name == "CANCEL":
            order = self._repo.broker_order(args["order_key"])
            if order is None:
                raise CommandError("취소 대상 주문 없음")
            return GateRequest.cancel(RequestSource.OPERATOR, order.account, order.instrument, Side(order.side),
                                      order.order_key, idem_key=idem, owner=None)
        order_type = OrderType(args.get("order_type", OrderType.MARKET.value))
        if order_type is OrderType.LIMIT and "price" not in args:
            raise CommandError("지정가 운영 요청에는 가격이 필요")
        price = Decimal(str(args.get("price", "1")))  # 시장가는 참조 가격만 (매도라 예약에 쓰지 않음)
        return GateRequest(
            source=RequestSource.OPERATOR, request_type=RequestType(name), account=args["account"],
            instrument=args["instrument"], side=Side.SELL, quantity=Decimal(str(args["quantity"])),
            order_type=order_type, price=price, idem_key=idem,
        )
