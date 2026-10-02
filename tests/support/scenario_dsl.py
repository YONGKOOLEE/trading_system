"""장애 시나리오 DSL과 시드 고정 무작위 사건열 (05-plan T-006, 04 D-070·V-95, 03 D-062, 05 D-080).

한 줄에 명령 하나. 무작위 실행기는 이 명령열을 만들어 실행하므로, 실패하면 시드와 명령열을 그대로 재현할 수 있다.

명령 (가격·수량은 가상값):
  behavior <FILL|ACCEPT_ONLY|REJECT|PRE_SEND_FAIL|UNKNOWN|LOST_ACCEPT|LOST_FILL>
  entry|increase|reduce|exit <종목> <수량>       전략 신호 → 게이트 → 승인 시 전송
  op-reduce|op-exit <종목> <수량>                 운영 요청
  cancel <n>                                       운영 요청: 열린 내부 주문 n번째(나머지 연산) 취소
  modify <n> <수량> <가격>                         실행 정책: 열린 내부 주문 n번째 정정(지정가)
  fill <n> [수량]                                  브로커: 열린 주문 n번째 체결 (수량 없으면 잔량 전부)
  expire                                           장 마감 소멸
  process                                          통보 반영
  dup | shuffle <시드> | drop                      대기 통보 중복 / 순서 섞기 / 유실
  query-fail <k> | query-delay <k>                 다음 조회 k회 실패 / 응답 유실 주문의 조회 반영 지연
  disconnect on|off | raise-after-send on|off
  resolve                                          결과 불명 의도 전부 조회 해소 (대기는 시계만 진행)
  halt <K> | resume <K> <LOCAL|REMOTE> | report <K> <조건> <0|1>
  kill-file on|off | storage-fail | restart | tick <초>
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from tests.scenario.conftest import ACCOUNT, Env
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType, IntentState, OrderType, RequestSource, RequestType, Side
from trading_system.domain.models import GateRequest, Signal
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.execution.resolver import UnknownResolver
from trading_system.halt.manager import HaltManager
from trading_system.storage.repository import StorageFailure

INSTRUMENTS = ("TST-A", "TST-B", "TST-C", "TST-D", "TST-E")
STRATEGY = "t006-random"
EXPECTED_ERRORS = (StorageFailure, ExecutionBlocked)


@dataclass
class Runner:
    env: Env
    log: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)  # 실행 중 확인한 안전 속성 위반
    eval_seq: int = 0
    cmd_seq: int = 0

    # --- 실행 -------------------------------------------------------------------
    def run(self, script: str | list[str]) -> None:
        lines = script.strip().splitlines() if isinstance(script, str) else script
        for line in lines:
            line = line.strip()
            if line and not line.startswith("#"):
                self.step(line)

    def step(self, line: str) -> None:
        self.log.append(line)
        cmd, *a = line.split()
        try:
            getattr(self, "_" + cmd.replace("-", "_"))(*a)
        except EXPECTED_ERRORS as exc:
            self.log.append(f"# 예상된 오류: {type(exc).__name__}")

    # --- 주문 요청 -----------------------------------------------------------------
    def _signal(self, kind: RequestType, inst: str, qty: str) -> None:
        self.eval_seq += 1
        now = self.env.clock.now()
        s = Signal(STRATEGY, 1, f"ev-{self.eval_seq}", ACCOUNT, inst, "R1", kind, Decimal(qty), OrderType.MARKET,
                   Decimal("10000"), now, now + timedelta(minutes=30))
        self._submit(GateRequest.from_signal(s))

    def _operator(self, kind: RequestType, inst: str, qty: str) -> None:
        self.cmd_seq += 1
        self._submit(GateRequest(source=RequestSource.OPERATOR, request_type=kind, account=ACCOUNT, instrument=inst,
                                 side=Side.SELL, quantity=Decimal(qty), order_type=OrderType.MARKET,
                                 price=Decimal("10000"), idem_key=f"op-{self.cmd_seq}"))

    def _submit(self, req: GateRequest) -> None:
        # 이 요청에 적용되는 정지: 범위 없는 정지, 같은 종목의 K10, 같은 전략의 K9 (K3 종목 범위는 위험 확대엔 무관)
        blocking = [h["halt_type"] for h in self.env.repo.halt_rows(ACCOUNT)
                    if (h["halt_type"] != "K10" or h["scope_instrument"] in (None, req.instrument))
                    and (h["halt_type"] != "K9" or h["scope_owner"] == req.owner)]
        d = self.env.gate.decide(req)
        if d.approved is None:
            return
        if req.is_risk_increasing and blocking:
            self.violations.append(f"정지 {blocking} 중 위험 확대 승인: {self.log[-1]}")
        sent_before = len(self.env.broker.sent)
        kill = self.env.kill_file.exists()
        self.env.executor.submit(d.approved)
        for r in self.env.broker.sent[sent_before:]:
            if kill and req.is_risk_increasing:
                self.violations.append(f"KILL 파일이 있는데 위험 확대 전송: {self.log[-1]}")
            if r.kind == "NEW" and r.side == "BUY" and not req.is_risk_increasing:
                self.violations.append(f"위험 축소 요청이 매수로 전송: {self.log[-1]}")

    def _entry(self, inst: str, qty: str) -> None:
        self._signal(RequestType.ENTRY, inst, qty)

    def _increase(self, inst: str, qty: str) -> None:
        self._signal(RequestType.INCREASE, inst, qty)

    def _reduce(self, inst: str, qty: str) -> None:
        self._signal(RequestType.REDUCE, inst, qty)

    def _exit(self, inst: str, qty: str) -> None:
        self._signal(RequestType.EXIT, inst, qty)

    def _op_reduce(self, inst: str, qty: str) -> None:
        self._operator(RequestType.REDUCE, inst, qty)

    def _op_exit(self, inst: str, qty: str) -> None:
        self._operator(RequestType.EXIT, inst, qty)

    def _open_internal(self) -> list:
        return [o for o in self.env.repo.open_orders(ACCOUNT)]

    def _cancel(self, n: str) -> None:
        orders = self._open_internal()
        if not orders:
            return
        o = orders[int(n) % len(orders)]
        self.cmd_seq += 1
        self._submit(GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, o.instrument, Side(o.side), o.order_key,
                                        idem_key=f"op-{self.cmd_seq}", owner=None))

    def _modify(self, n: str, qty: str, price: str) -> None:
        orders = self._open_internal()
        if not orders:
            return
        o = orders[int(n) % len(orders)]
        owner = self.env.repo.intent(o.intent_id).owner if o.intent_id else None
        self.cmd_seq += 1
        self._submit(GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY, account=ACCOUNT,
                                 instrument=o.instrument, side=Side(o.side), quantity=Decimal(qty),
                                 order_type=OrderType.LIMIT, price=Decimal(price), idem_key=f"ep-{self.cmd_seq}",
                                 owner=owner, target_order_key=o.order_key))

    # --- 브로커·통보 ----------------------------------------------------------------
    def _behavior(self, name: str) -> None:
        self.env.broker.behavior = FakeBehavior(name)

    def _fill(self, n: str, qty: str | None = None) -> None:
        nos = self.env.broker.open_order_nos()
        if not nos:
            return
        no = nos[int(n) % len(nos)]
        rec = next(r for r in self.env.broker.query_orders_unchecked() if r.order_no == no)
        left = rec.quantity - rec.filled_quantity
        q = None if qty is None else min(Decimal(qty), left)
        self.env.broker.fill(no, q)

    def _expire(self) -> None:
        self.env.broker.expire_open()

    def _process(self) -> None:
        self.env.executor.process_events()

    def _dup(self) -> None:
        self.env.broker.duplicate_events()

    def _shuffle(self, seed: str) -> None:
        self.env.broker.shuffle_events(random.Random(int(seed)))

    def _drop(self) -> None:
        self.env.broker.drop_events()

    def _query_fail(self, k: str) -> None:
        self.env.broker.query_failures = int(k)

    def _query_delay(self, k: str) -> None:
        self.env.broker.query_delay = int(k)

    def _disconnect(self, on: str) -> None:
        self.env.broker.disconnected = on == "on"

    def _raise_after_send(self, on: str) -> None:
        self.env.broker.raise_after_send = on == "on"

    def _resolve(self) -> None:
        r = UnknownResolver(self.env.repo, self.env.broker, self.env.executor, self.env.clock,
                            lambda s: self.env.clock.advance(timedelta(seconds=s)))
        for i in self.env.repo.intents_in_state(IntentState.UNKNOWN):
            if self.env.repo.intent(i.intent_id).state is IntentState.UNKNOWN:
                r.resolve(i.intent_id)

    # --- 정지·환경 --------------------------------------------------------------------
    def _halt(self, k: str) -> None:
        self.env.halts.activate(HaltType(k), "dsl", "무작위", ACCOUNT,
                                instrument="TST-A" if k in ("K3", "K10") else None,
                                owner=f"{STRATEGY}:1" if k == "K9" else None, variant="USER" if k == "K9" else None)

    def _resume(self, k: str, channel: str) -> None:
        self.env.halts.resume(HaltType(k), ACCOUNT, channel, "dsl")

    def _report(self, k: str, cond: str, ok: str) -> None:
        self.env.halts.report_condition(HaltType(k), ACCOUNT, cond, ok == "1")

    def _kill_file(self, on: str) -> None:
        if on == "on":
            self.env.kill_file.write_text("KILL")
        elif self.env.kill_file.exists():
            self.env.kill_file.unlink()

    def _storage_fail(self) -> None:
        self.env.repo.fail_next_transaction = True

    def _restart(self) -> None:
        """프로세스 재시작: 메모리 상태(실행기 차단·미처리 통보)를 버리고 복구 절차를 밟는다."""
        self.env.repo.fail_next_transaction = False
        self.env.executor = Executor(self.env.repo, self.env.broker, self.env.clock, self.env.kill_file)
        self.env.halts = HaltManager(self.env.repo, self.env.gate, self.env.executor, self.env.clock,
                                     reference_time_of=lambda a: None)
        self.env.executor.recover_on_start()
        self.env.halts.on_start()

    def _tick(self, seconds: str) -> None:
        self.env.clock.advance(timedelta(seconds=int(seconds)))


# --- 무작위 명령열 생성 ----------------------------------------------------------------------

_WEIGHTS: list[tuple[str, int]] = [
    ("behavior", 8), ("entry", 12), ("increase", 5), ("reduce", 6), ("exit", 5), ("op-reduce", 3), ("op-exit", 3),
    ("cancel", 5), ("modify", 6), ("fill", 10), ("expire", 2), ("process", 12), ("dup", 3), ("shuffle", 3),
    ("drop", 1), ("query-fail", 2), ("query-delay", 2), ("disconnect", 2), ("raise-after-send", 2), ("resolve", 5),
    ("halt", 1), ("resume", 3), ("report", 2), ("kill-file", 2), ("storage-fail", 1), ("restart", 1), ("tick", 3),
]
_K = [k.value for k in HaltType]
_CONDS = ["reconciled", "broker_connected", "clock_ok", "storage_ok", "recovery_ok", "data_ok", "notify_ok"]


def random_script(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    names = [w[0] for w in _WEIGHTS]
    weights = [w[1] for w in _WEIGHTS]
    out = []
    for _ in range(n):
        c = rng.choices(names, weights)[0]
        inst = rng.choice(INSTRUMENTS)
        qty = str(rng.choice([1, 5, 10, 30, 50, 100, 150, 250]))
        if c == "behavior":
            # 미체결·체결을 자주 만들어 취소·정정·늦은 체결 경로를 지나게 한다
            out.append("behavior " + rng.choices([b.value for b in FakeBehavior], [3, 6, 1, 1, 1, 2, 2])[0])
        elif c in ("entry", "increase", "reduce", "exit", "op-reduce", "op-exit"):
            out.append(f"{c} {inst} {qty}")
        elif c == "cancel":
            out.append(f"cancel {rng.randrange(5)}")
        elif c == "modify":
            out.append(f"modify {rng.randrange(5)} {qty} {rng.choice(['9900', '9950', '9995', '10000'])}")
        elif c == "fill":
            out.append(f"fill {rng.randrange(5)}" + ("" if rng.random() < 0.5 else f" {qty}"))
        elif c == "shuffle":
            out.append(f"shuffle {rng.randrange(10_000)}")
        elif c in ("query-fail", "query-delay"):
            out.append(f"{c} {rng.randrange(4)}")
        elif c in ("disconnect", "raise-after-send", "kill-file"):
            out.append(f"{c} {rng.choice(['on', 'off'])}")
        elif c == "halt":
            out.append(f"halt {rng.choice(_K)}")
        elif c == "resume":
            out.append(f"resume {rng.choice(_K)} {rng.choice(['LOCAL', 'REMOTE'])}")
        elif c == "report":
            out.append(f"report {rng.choice(_K)} {rng.choice(_CONDS)} {rng.choice(['0', '1'])}")
        elif c == "tick":
            out.append(f"tick {rng.choice([1, 10, 60, 300])}")
        else:
            out.append(c)
    return out
