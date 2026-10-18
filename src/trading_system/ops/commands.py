"""운영 명령 처리 (R-07, R-21, 01 §2.9, 03 §6.6).

로컬 CLI·메신저(T-013) → DB 명령 테이블(명령 ID = 멱등키) → 이 처리기 → 정지 관리 또는 게이트.
- 운영 요청은 취소·감액·청산만 (신규 진입·증액 불가, R-21). 요청은 반드시 게이트를 거친다.
- 원격 채널: 킬 스위치 발동·K1/K2 재개·취소·감액·청산·대조. K4/K8 재개는 명령 처리 중 새 대조를 거쳐서만 (CP-026).
  결과 불명 확정·불일치 해소는 로컬만.
- 자동 운영 채널(AUTO, PAPER 무인 자동 운영 CP-029): K8 재개만. 원격 K8과 같은 새 대조 기준 + 명령의 시한(not_after).
- 원격 인증(허용 채팅 ID·확인 코드)은 T-013. 이 처리기는 기록된 채널 값을 믿는다.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from collections.abc import Callable
from typing import Protocol
from decimal import Decimal, InvalidOperation

from trading_system.domain.clock import Clock, from_iso
from trading_system.domain.enums import GateResult, HaltType, OrderType, RequestSource, RequestType, Side
from trading_system.domain.halts import REMOTE_FRESH_RECONCILE
from trading_system.domain.models import GateRequest
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.gate.gate import Gate
from trading_system.halt.manager import Channel, HaltManager
from trading_system.storage.repository import Repository

LOCAL_ONLY = frozenset({"CONFIRM_UNKNOWN", "RECONCILE_RESOLVE", "STRATEGY_SYNC"})
REMOTE_RESUME_TRIGGER = "REMOTE_RESUME"   # 원격 K4·K8 재개 직전 대조의 사유 (CP-026)
AUTO_RESUME_TRIGGER = "AUTO_RESUME"       # 자동 운영 K8 재개 직전 대조의 사유 (CP-029 D-152) — 감사에서 원격과 구분
SUPERSEDED_BY_KILL = "뒤이은 KILL 명령이 있어 K1 재개 거부 (사용자의 마지막 의사 = KILL)"
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
        # [CP-036 D-211] 운영 요청의 게이트 전 시장별 주문 조건 훅(M2: 유형·가격·dust·수량 단위). 거부는 ValueError.
        # M1은 None — 기존 동작 그대로
        self.prepare: Callable[[GateRequest], GateRequest] | None = None
        # [CP-037 D-226] 자동 운영 채널(AUTO) K8 재개의 시장별 추가 조건(M2: 서버 시각·규칙·사용자 스트림·시세 신선도).
        # 사유 문자열이면 새 대조 없이 거부. M1은 None — 기존 동작 그대로. 원격·로컬 재개에는 쓰지 않는다
        self.auto_resume_check: Callable[[], str | None] | None = None

    def process_pending(self) -> list[CommandOutcome]:
        """대기 명령을 처리한다. 킬 스위치를 먼저 처리하고, 명령 하나의 실패가 나머지를 막지 않게 한다 (CP-018 R-4)."""
        cmds = sorted(self._repo.pending_commands(), key=lambda c: (c["command"] != "KILL", c["created_seq"]))
        # KILL을 먼저 처리하므로, 같은 묶음에서 KILL보다 먼저 들어온 RESUME K1이 그 KILL을 풀어 버리는 역전을 막는다
        # (재검토2 #4): 그런 RESUME은 거부 — 사용자의 마지막 의사(KILL)를 따른다
        kills = [c for c in cmds if c["command"] == "KILL"]
        return [self._superseded(cmd) if self._resume_k1_before(cmd, kills) else self._process(cmd)
                for cmd in cmds]

    @staticmethod
    def _args(cmd: dict) -> dict | None:
        try:
            a = json.loads(cmd["args"])
        except ValueError:
            return None
        return a if isinstance(a, dict) else None

    @classmethod
    def _kill_applies(cls, kill: dict, resume_account: str | None) -> bool:
        """이 KILL이 그 RESUME K1의 계좌에 걸리는가 — 형식 오류 KILL은 세지 않는다 (4차 검토 F-E).
        이 RESUME이 풀 K1 행에 그 KILL의 행이 포함되면 걸린다: 전체 계좌 KILL(None)은 모든 RESUME에, 전체 계좌
        RESUME(None)은 계좌 K1 행까지 풀므로 모든 KILL에(5차 검토 F-1), 계좌 RESUME은 같은 계좌 KILL에만."""
        a = cls._args(kill)
        if a is None or kill["channel"] not in (Channel.LOCAL, Channel.REMOTE):
            return False
        acct = a.get("account")
        return acct is None or resume_account is None or acct == resume_account

    @classmethod
    def _resume_k1_before(cls, cmd: dict, kills: list[dict]) -> bool:
        if cmd["command"] != "RESUME":
            return False
        a = cls._args(cmd)
        if a is None or a.get("halt_type") != "K1":
            return False
        return any(k["created_seq"] > cmd["created_seq"] and cls._kill_applies(k, a.get("account")) for k in kills)

    def _superseded(self, cmd: dict) -> CommandOutcome:
        cid, actor = cmd["command_id"], cmd["actor"]
        note = SUPERSEDED_BY_KILL
        if cmd["channel"] == Channel.REMOTE:
            self._executor.alert("WARNING", f"원격 명령 RESUME REJECTED: {note}", cid)
        with self._repo.tx():
            now = self._clock.now()
            self._repo.finish_command(cid, "REJECTED", note, now)
            self._repo.audit(now, "OPS_COMMAND", actor, cid,
                             {"channel": cmd["channel"], "command": "RESUME", "status": "REJECTED", "result": note})
        return CommandOutcome(cid, "REJECTED", note)

    def _process(self, cmd: dict) -> CommandOutcome:
        cid, channel, actor, name = cmd["command_id"], cmd["channel"], cmd["actor"], cmd["command"]
        try:
            args = json.loads(cmd["args"])
            if name not in KNOWN:
                raise CommandError(f"알 수 없는 명령 {name}")
            if channel not in (Channel.LOCAL, Channel.REMOTE, Channel.AUTO):
                raise CommandError(f"알 수 없는 채널 {channel}")
            if channel == Channel.AUTO and not (name == "RESUME" and isinstance(args, dict)
                                                and args.get("halt_type") == HaltType.K8.value):
                raise CommandError("자동 운영 채널(AUTO)은 K8 재개만 할 수 있음 (CP-029)")
            if name in LOCAL_ONLY and channel != Channel.LOCAL:
                raise CommandError(f"{name}은 로컬에서만 가능")
            status, result = self._dispatch(cid, channel, actor, name, args, cmd.get("created_seq"))
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

    def _dispatch(self, cid: str, channel: str, actor: str, name: str, args: dict,
                  seq: int | None = None) -> tuple[str, str]:
        account = args.get("account")
        if name == "KILL":
            halt_id = self._halts.activate(HaltType.K1, f"{channel}:{actor}", args.get("reason") or "운영자 킬 스위치",
                                           account)
            return "DONE", f"K1 발동 {halt_id}"
        if name == "RESUME":
            halt_type = HaltType(args["halt_type"])
            fresh = None
            if channel == Channel.AUTO:
                expired = self._auto_expired(args)   # 시한이 지난 자동 재개는 대조도 하지 않고 거부 (CP-029 D-153)
                if expired:
                    return "REJECTED", expired
                if self.auto_resume_check is not None:   # [CP-037 D-226] 대조 전에 — 미충족이면 새 대조 없이 거부
                    unmet = self.auto_resume_check()
                    if unmet:
                        return "REJECTED", unmet
            if channel in (Channel.REMOTE, Channel.AUTO) and halt_type in REMOTE_FRESH_RECONCILE:
                trigger = AUTO_RESUME_TRIGGER if channel == Channel.AUTO else REMOTE_RESUME_TRIGGER
                fresh, refused = self._fresh_reconcile(account, trigger)
                if refused:
                    return "REJECTED", refused
            # 대기 명령을 읽은 뒤 들어온 KILL(감시 스레드 fast_kill은 기존 K1을 보고 행을 남기지 않음)도 RESUME 적용
            # 저장 단위 안에서 다시 확인한다 — 풀린 K1로 같은 tick에 신규 진입이 나가지 않게 (3차 검토 #2)
            check = (lambda: self._later_kill(seq)) if halt_type is HaltType.K1 and seq is not None else None
            if channel == Channel.AUTO:
                # 새 대조가 오래 걸려 시한을 넘겼으면 재개 저장 단위 안에서 거부 — 자동 운영이 "08:55 시한 경과"를
                # 알린 뒤 K8이 풀리지 않게 (CP-029 재검토 M2)
                check = lambda: self._auto_expired(args)  # noqa: E731
            r = self._halts.resume(halt_type, account, channel, actor, superseded=check, fresh_recon=fresh)
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
            return "DONE", f"대조 {getattr(r, 'result', r)}, 열린 불일치 {len(self._repo.open_recon_items(args['account']))}건"
        if name == "RECONCILE_RESOLVE":
            if self._resolver is None:
                raise CommandError("해소기가 연결되지 않음")
            return "DONE", self._resolver.resolve(args["item_id"], args["method"], actor)
        if name == "STRATEGY_SYNC":  # 전략 도구가 남긴 상태 전이를 K9에 반영 (RV-20, 01 K9, INV-13)
            if self._registry is None:
                raise CommandError("전략 등록부가 연결되지 않음")
            return "DONE", self._registry.sync_k9_from_state(str(args["owner"]), f"{channel}:{actor}")
        req = self._request(cid, name, args)
        if self.prepare is not None:
            try:
                req = self.prepare(req)
            except ValueError as exc:   # 게이트 전 시장별 거부(M2 D-211)도 게이트 거부와 같은 긴급 알림 (재검토)
                self._executor.alert("EMERGENCY", f"운영 요청 거부 ({name}): {exc}", cid)
                raise
        d = self._gate.decide(req)
        if d.result is not GateResult.APPROVED or d.approved is None:
            self._executor.alert("EMERGENCY", f"운영 요청 거부 ({name}): {d.reason}", cid)  # 위험 축소 거부는 알림
            return "REJECTED", f"게이트 거부 {d.failed_check}: {d.reason}"
        state = self._executor.submit(d.approved)
        return "DONE", f"의도 {d.approved.intent_id} {state.value}"

    def _auto_expired(self, args: dict) -> str | None:
        """자동 운영 K8 재개의 시한(args.not_after, ISO 시각). 없거나 지났으면 거부 사유 (CP-029 D-153).
        자동 운영이 08:55 시한 뒤 알림을 보낸 다음에, 늦게 처리된 명령이 K8을 푸는 일을 막는다."""
        raw = args.get("not_after")
        if not isinstance(raw, str):
            return "자동 재개 명령에 시한(not_after)이 없음 — 처리하지 않음"
        try:
            limit = from_iso(raw)
        except ValueError:
            return "자동 재개 시한 형식 오류 — 처리하지 않음"
        if self._clock.now() > limit:
            return f"자동 재개 시한 경과({raw}) — 처리하지 않음"
        return None

    def _fresh_reconcile(self, account: str | None,
                         trigger: str = REMOTE_RESUME_TRIGGER) -> tuple[str | None, str | None]:
        """원격·자동 K4·K8 재개 직전에 대조를 새로 한 번 한다 (A-069 ①②, CP-029). (대조 ID, 거부 사유).
        일치·열린 불일치 0건만 통과. K8은 대조 뒤 엔진의 복구 판정(engine._after_reconcile)이 이 대조 ID로
        recovery_ok를 보고해야 재개된다."""
        if not account:
            raise CommandError("원격·자동 K4·K8 재개에는 계좌가 필요")
        if self._reconciler is None:
            raise CommandError("대조기가 연결되지 않음")
        r = self._reconciler.run(account, trigger)
        result, rid = getattr(r, "result", None), getattr(r, "recon_id", None)
        open_items = len(self._repo.open_recon_items(account))
        if result != "MATCH" or open_items or not rid:
            return None, f"새 대조 {result}, 열린 불일치 {open_items}건 — 재개하지 않음 (해소는 PC에서 reconcile-resolve)"
        return rid, None

    def _later_kill(self, seq: int) -> str | None:
        """이 RESUME(created_seq=seq)보다 뒤에 들어온 대기 KILL이 있으면 거부 사유 (resume의 저장 단위 안에서 호출)."""
        mine = next((c for c in self._repo.pending_commands() if c["created_seq"] == seq), None)
        account = (self._args(mine) or {}).get("account") if mine else None
        if any(c["command"] == "KILL" and c["created_seq"] > seq and self._kill_applies(c, account)
               for c in self._repo.pending_commands()):
            return SUPERSEDED_BY_KILL
        return None

    def _request(self, cid: str, name: str, args: dict) -> GateRequest:
        idem = f"ops:{cid}"
        if name == "CANCEL":
            order = self._repo.broker_order(args["order_key"])
            if order is None:
                raise CommandError("취소 대상 주문 없음")
            return GateRequest.cancel(RequestSource.OPERATOR, order.account, order.instrument, Side(order.side),
                                      order.order_key, idem_key=idem, owner=None)
        order_type = OrderType(args.get("order_type", OrderType.MARKET.value))
        if order_type is OrderType.LIMIT:
            # T-027 전에는 호가 표가 없어 G10이 지정가를 모두 거부했다. 표(krx.toml)가 생긴 뒤에도 운영 요청 지정가는
            # 모의 지정가 실측(CP-032 Q-3) 승인 전까지 받지 않는다 — 입력 경로(CLI·텔레그램)도 없다 (A-085)
            raise CommandError("지정가 운영 요청은 받지 않음 (CP-032 Q-3 승인 전) — 시장가로 요청")
        price = Decimal(str(args.get("price", "1")))  # 시장가는 참조 가격만 (매도라 예약에 쓰지 않음)
        return GateRequest(
            source=RequestSource.OPERATOR, request_type=RequestType(name), account=args["account"],
            instrument=args["instrument"], side=Side.SELL, quantity=Decimal(str(args["quantity"])),
            order_type=order_type, price=price, idem_key=idem,
        )
