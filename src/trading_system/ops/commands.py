"""운영 명령 처리 (R-07, R-21, 01 §2.9, 03 §6.6).

로컬 CLI·메신저(T-013) → DB 명령 테이블(명령 ID = 멱등키) → 이 처리기 → 정지 관리 또는 게이트.
- 운영 요청은 취소·감액·청산만 (신규 진입·증액 불가, R-21). 요청은 반드시 게이트를 거친다.
- 원격 채널: 킬 스위치 발동·K1/K2 재개·취소·감액·청산. 결과 불명 확정은 로컬만.
- 원격 인증(허용 채팅 ID·확인 코드)은 T-013. 이 처리기는 기록된 채널 값을 믿는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol
from decimal import Decimal, InvalidOperation

from trading_system.domain.clock import Clock
from trading_system.domain.enums import GateResult, HaltType, OrderType, RequestSource, RequestType, Side
from trading_system.domain.models import GateRequest
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.gate.gate import Gate
from trading_system.halt.manager import Channel, HaltManager
from trading_system.storage.repository import Repository

LOCAL_ONLY = frozenset({"CONFIRM_UNKNOWN", "RECONCILE", "RECONCILE_RESOLVE", "STRATEGY_SYNC"})
KNOWN = frozenset({"KILL", "RESUME", "CANCEL", "REDUCE", "EXIT", "CONFIRM_UNKNOWN", "RECONCILE", "RECONCILE_RESOLVE",
                   "STRATEGY_SYNC"})


class _Reconciles(Protocol):
    def run(self, account: str, trigger: str) -> object: ...


class _Resolves(Protocol):
    def resolve(self, item_id: str, method: str, actor: str) -> str: ...


class _SyncsStrategy(Protocol):
    def sync_k9_from_state(self, owner: str, actor: str) -> str: ...


class CommandError(ValueError):
    pass


@dataclass(frozen=True)
class CommandOutcome:
    command_id: str
    status: str  # DONE / REJECTED / FAILED
    result: str


class OpsProcessor:
    def __init__(self, repo: Repository, gate: Gate, executor: Executor, halts: HaltManager, clock: Clock, *,
                 reconciler: _Reconciles | None = None, resolver: _Resolves | None = None,
                 kill_file: Path | None = None, registry: _SyncsStrategy | None = None) -> None:
        self._kill_file = kill_file
        self._registry = registry
        self._reconciler = reconciler
        self._resolver = resolver
        self._repo = repo
        self._gate = gate
        self._executor = executor
        self._halts = halts
        self._clock = clock

    def process_pending(self) -> list[CommandOutcome]:
        """대기 명령을 처리한다. 킬 스위치를 먼저 처리하고, 명령 하나의 실패가 나머지를 막지 않게 한다 (CP-018 R-4)."""
        cmds = sorted(self._repo.pending_commands(), key=lambda c: (c["command"] != "KILL", c["created_seq"]))
        return [self._process(cmd) for cmd in cmds]

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
        except Exception as exc:  # noqa: BLE001 — 형식이 잘못된 인자·저장 오류 등. 큐를 막지 않고 실패로 기록
            status, result = "FAILED", f"{type(exc).__name__}: {exc}"
            self._executor.alert("EMERGENCY", f"운영 명령 처리 실패 ({name}): {type(exc).__name__}", cid)
        if channel == Channel.REMOTE:  # 원격 명령 결과는 같은 채널로 회신 (03 §6.6)
            self._executor.alert("INFO" if status == "DONE" else "WARNING", f"원격 명령 {name} {status}: {result}", cid)
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
            note = r.reason
            if self._kill_file is not None and self._kill_file.exists():
                note += " / KILL 파일 있음: 위험 확대 전송은 계속 차단 (로컬에서만 삭제, V-119)"
            return ("DONE" if r.ok else "REJECTED"), note
        if name == "CONFIRM_UNKNOWN":
            accepted = args["accepted"]
            if not isinstance(accepted, bool):  # 문자열 "false"가 참이 되는 것을 막음
                raise CommandError("accepted는 true/false(불리언)여야 함")
            self._halts.confirm_unknown(args["intent_id"], accepted, actor, args.get("order_no"))
            return "DONE", "결과 불명 확정"
        if name == "RECONCILE":
            if self._reconciler is None:
                raise CommandError("대조기가 연결되지 않음")
            r = self._reconciler.run(args["account"], "MANUAL")
            return "DONE", f"대조 {getattr(r, 'result', r)}"
        if name == "RECONCILE_RESOLVE":
            if self._resolver is None:
                raise CommandError("해소기가 연결되지 않음")
            return "DONE", self._resolver.resolve(args["item_id"], args["method"], actor)
        if name == "STRATEGY_SYNC":  # 전략 도구가 남긴 상태 전이를 K9에 반영 (RV-20, 01 K9, INV-13)
            if self._registry is None:
                raise CommandError("전략 등록부가 연결되지 않음")
            return "DONE", self._registry.sync_k9_from_state(str(args["owner"]), f"{channel}:{actor}")
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
