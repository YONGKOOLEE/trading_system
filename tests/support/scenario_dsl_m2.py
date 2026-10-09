"""M2 장애 시나리오 DSL과 시드 고정 무작위 사건열 (05 T-M2-09, 04 V-157(V-95 M2판)·D-070, CP-024 D-099·D-104·D-112).

M1 DSL(tests/support/scenario_dsl.py)과 같은 형식(한 줄 = 명령 하나, 실패하면 시드·명령열로 재현). 값은 모두 가상이다.
M1과 다른 점: 가상 크립토 심볼·소수 수량, 의도 생성 시 M2 주문 유형·가격 규칙(매수 IOC 지정가, 매도 시장가 / 시세 지연 중
IOC 지정가, D-104·D-120), 정정 없음(요청하면 게이트 G10 거부가 기대값), M2 해소기·대조기, M2 장애:
  reply <코드>      다음 신규 주문 1건의 합성 응답 (-1013·-1021·-2010·-2010dup·429·418·5xx·5xx+·403·-1022, D-112)
  ioc on|off        지정가를 IOC로 처리 (기본 on)
  liquidity <심볼> <수량|all>  IOC 즉시 체결 가능 수량
  stale <심볼> on|off  시세 지연(60초 초과)
  stream-drop       사용자 스트림 끊김·serverShutdown: 대기 통보 유실 + 재연결 표시 → 다음 reconcile은 재연결 대조
  reset             테스트넷 리셋 모의 (활성 주문 소실 + 잔고 재지급)
  reconcile         M2 대조 (재연결 표시가 있으면 STREAM_RECONNECT)
  resolve           M2 결과 불명 해소 (대기는 시계만 진행)
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal

from tests.support.m2_fakes import (
    ACCOUNT, M2_ORDER_POLICY, PAIRS, QUOTE_ASSET, RULES, M2Env, asset_pair, symbol_rules,
)
from trading_system.broker.fake.broker import FakeBehavior
from trading_system.domain.enums import HaltType, IntentState, OrderType, RequestSource, RequestType, Side
from trading_system.domain.models import GateRequest, Signal
from trading_system.domain.trading_rules import apply_spot_terms
from trading_system.execution.executor import ExecutionBlocked, Executor
from trading_system.execution.spot_resolver import SpotUnknownResolver
from trading_system.halt.manager import HaltManager
from trading_system.reconciliation.spot import PERIODIC, STREAM_RECONNECT, SpotReconciler
from trading_system.storage.alerts import AuditAlertSink
from trading_system.storage.repository import StorageFailure

INSTRUMENTS = ("TSTA-USDT", "TSTB-USDT", "TSTC-USDT")
QUANTITIES = {"TSTA-USDT": ("0.0100", "0.0500", "0.1000", "0.1500", "0.0999"),
              "TSTB-USDT": ("10.0", "50.0", "100.0", "400.0", "99.9"),
              "TSTC-USDT": ("10.0", "50.0", "100.0", "400.0", "99.9")}
REPLIES = ("-1013", "-1021", "-2010", "-2010dup", "429", "418", "5xx", "5xx+", "403", "-1022")
STRATEGY = "tm209-random"
EXPECTED_ERRORS = (StorageFailure, ExecutionBlocked)


@dataclass
class M2Runner:
    env: M2Env
    log: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)
    eval_seq: int = 0
    cmd_seq: int = 0
    reconnected: bool = False
    _rec: SpotReconciler | None = None

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

    # --- 주문 요청 (의도 생성 시 M2 유형·가격 규칙, 게이트 전) -------------------------------------------
    def _terms(self, req: GateRequest) -> GateRequest | None:
        now = self.env.clock.now()
        return apply_spot_terms(req, self.env.market.quote(req.instrument), now,
                                self.env.rules.rules(req.instrument), M2_ORDER_POLICY)

    def _signal(self, kind: RequestType, inst: str, qty: str) -> None:
        self.eval_seq += 1
        now = self.env.clock.now()
        s = Signal(STRATEGY, 1, f"ev-{self.eval_seq}", ACCOUNT, inst, "R1", kind, Decimal(qty), OrderType.MARKET,
                   RULES[inst].last, now, now + timedelta(minutes=30))
        self._submit(GateRequest.from_signal(s))

    def _operator(self, kind: RequestType, inst: str, qty: str) -> None:
        self.cmd_seq += 1
        self._submit(GateRequest(source=RequestSource.OPERATOR, request_type=kind, account=ACCOUNT, instrument=inst,
                                 side=Side.SELL, quantity=Decimal(qty), order_type=OrderType.MARKET,
                                 price=RULES[inst].last, idem_key=f"op-{self.cmd_seq}"))

    def _submit(self, req: GateRequest) -> None:
        if req.request_type.is_new_order:
            terms = self._terms(req)
            if terms is None:
                return   # 유형·가격을 정할 수 없음 → 의도를 만들지 않음 (기록은 호출자 몫)
            req = terms
        if self.env.dust.screen_exit(req):
            return
        blocking = [h["halt_type"] for h in self.env.repo.halt_rows(ACCOUNT)
                    if (h["halt_type"] != "K10" or h["scope_instrument"] in (None, req.instrument))
                    and (h["halt_type"] != "K9" or h["scope_owner"] == req.owner)]
        d = self.env.gate.decide(req)
        if d.approved is None:
            return
        if req.request_type is RequestType.MODIFY:
            self.violations.append(f"M2 정정 승인: {self.log[-1]}")
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
            if r.kind == "NEW" and r.side == "BUY" and r.order_type is OrderType.MARKET:
                self.violations.append(f"M2 시장가 매수 전송: {self.log[-1]}")
            if r.kind == "MODIFY":
                self.violations.append(f"M2 정정 전송: {self.log[-1]}")

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

    def _cancel(self, n: str) -> None:
        orders = self.env.repo.open_orders(ACCOUNT)
        if not orders:
            return
        o = orders[int(n) % len(orders)]
        self.cmd_seq += 1
        self._submit(GateRequest.cancel(RequestSource.OPERATOR, ACCOUNT, o.instrument, Side(o.side), o.order_key,
                                        idem_key=f"op-{self.cmd_seq}", owner=None))

    def _modify(self, n: str) -> None:
        """M2 정정 미지원 (D-104) — 요청이 들어와도 게이트가 거부해야 한다."""
        orders = self.env.repo.open_orders(ACCOUNT)
        if not orders:
            return
        o = orders[int(n) % len(orders)]
        owner = self.env.repo.intent(o.intent_id).owner if o.intent_id else None
        self.cmd_seq += 1
        self._submit(GateRequest(source=RequestSource.EXEC_POLICY, request_type=RequestType.MODIFY, account=ACCOUNT,
                                 instrument=o.instrument, side=Side(o.side), quantity=o.quantity - o.filled_quantity,
                                 order_type=OrderType.LIMIT, price=RULES[o.instrument].last,
                                 idem_key=f"ep-{self.cmd_seq}", owner=owner, target_order_key=o.order_key))

    # --- 브로커·통보·장애 ---------------------------------------------------------------------
    def _behavior(self, name: str) -> None:
        self.env.broker.behavior = FakeBehavior(name)

    def _ioc(self, on: str) -> None:
        self.env.broker.ioc_limit = on == "on"

    def _liquidity(self, inst: str, qty: str) -> None:
        if qty == "all":
            self.env.broker.ioc_liquidity.pop(inst, None)
        else:
            self.env.broker.ioc_liquidity[inst] = Decimal(qty)

    def _reply(self, code: str) -> None:
        self.env.broker.replies.append(code)

    def _stale(self, inst: str, on: str) -> None:
        if on == "on":
            self.env.market.quote_age[inst] = timedelta(seconds=120)
        else:
            self.env.market.quote_age.pop(inst, None)

    def _fill(self, n: str, qty: str | None = None) -> None:
        nos = self.env.broker.open_order_nos()
        if not nos:
            return
        no = nos[int(n) % len(nos)]
        rec = next(r for r in self.env.broker.query_orders_unchecked() if r.order_no == no)
        left = rec.quantity - rec.filled_quantity
        step = symbol_rules(rec.instrument).lot_step
        q = None if qty is None else min((Decimal(qty) / step).to_integral_value() * step, left)
        if q is not None and q <= 0:
            return
        self.env.broker.fill(no, q)

    def _process(self) -> None:
        self.env.executor.process_events()

    def _dup(self) -> None:
        self.env.broker.duplicate_events()

    def _shuffle(self, seed: str) -> None:
        self.env.broker.shuffle_events(random.Random(int(seed)))

    def _drop(self) -> None:
        self.env.broker.drop_events()

    def _stream_drop(self) -> None:
        self.env.broker.drop_events()
        self.reconnected = True

    def _query_fail(self, k: str) -> None:
        self.env.broker.query_failures = int(k)

    def _query_delay(self, k: str) -> None:
        self.env.broker.query_delay = int(k)

    def _disconnect(self, on: str) -> None:
        self.env.broker.disconnected = on == "on"

    def _raise_after_send(self, on: str) -> None:
        self.env.broker.raise_after_send = on == "on"

    def _reset(self) -> None:
        self.env.broker.reset({QUOTE_ASSET: Decimal("10000")})

    def _resolve(self) -> None:
        r = SpotUnknownResolver(self.env.repo, self.env.broker, self.env.executor, self.env.clock,
                                lambda s: self.env.clock.advance(timedelta(seconds=s)))
        for i in self.env.repo.intents_in_state(IntentState.UNKNOWN):
            if self.env.repo.intent(i.intent_id).state is IntentState.UNKNOWN:
                r.resolve(i.intent_id)

    def _reconcile(self) -> None:
        """같은 대조기를 계속 쓴다(연속 조회 실패 수 → K5). 재시작하면 새로 만든다."""
        if self._rec is None:
            expected = getattr(self.env.account, "expected_quote", lambda a: None)
            self._rec = SpotReconciler(self.env.repo, self.env.broker, self.env.clock, self.env.halts,
                                       AuditAlertSink(self.env.repo, self.env.clock), universe=PAIRS,
                                       quote_asset=QUOTE_ASSET, expected_quote=expected,
                                       drain_events=lambda: self.env.executor.process_events())
        self._rec.run(ACCOUNT, STREAM_RECONNECT if self.reconnected else PERIODIC)
        self.reconnected = False

    # --- 정지·환경 ----------------------------------------------------------------------------
    def _halt(self, k: str) -> None:
        self.env.halts.activate(HaltType(k), "dsl", "무작위", ACCOUNT,
                                instrument="TSTA-USDT" if k in ("K3", "K10") else None,
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
        self.env.repo.fail_next_transaction = False
        dust_listener = self.env.dust.after_reject
        self.env.executor = Executor(self.env.repo, self.env.broker, self.env.clock, self.env.kill_file,
                                     asset_pair=asset_pair, client_ids=True)
        self.env.executor.reject_listeners.append(dust_listener)
        self.env.halts = HaltManager(self.env.repo, self.env.gate, self.env.executor, self.env.clock,
                                     reference_time_of=lambda a: None)
        self.env.executor.recover_on_start()
        self.env.halts.on_start()
        self._rec = None

    def _tick(self, seconds: str) -> None:
        self.env.clock.advance(timedelta(seconds=int(seconds)))


_WEIGHTS: list[tuple[str, int]] = [
    ("behavior", 8), ("entry", 12), ("increase", 5), ("reduce", 6), ("exit", 5), ("op-reduce", 3), ("op-exit", 3),
    ("cancel", 3), ("modify", 2), ("fill", 8), ("process", 12), ("dup", 3), ("shuffle", 3), ("drop", 1),
    ("query-fail", 2), ("query-delay", 2), ("disconnect", 2), ("raise-after-send", 2), ("resolve", 5),
    ("halt", 1), ("resume", 3), ("report", 2), ("kill-file", 2), ("storage-fail", 1), ("restart", 1), ("tick", 3),
    ("reply", 5), ("ioc", 2), ("liquidity", 2), ("stale", 2), ("stream-drop", 2), ("reset", 1), ("reconcile", 3),
]
_K = [k.value for k in HaltType]
_CONDS = ["reconciled", "broker_connected", "clock_ok", "storage_ok", "recovery_ok", "data_ok", "notify_ok"]


def random_script_m2(seed: int, n: int) -> list[str]:
    rng = random.Random(seed)
    names = [w[0] for w in _WEIGHTS]
    weights = [w[1] for w in _WEIGHTS]
    out = []
    for _ in range(n):
        c = rng.choices(names, weights)[0]
        inst = rng.choice(INSTRUMENTS)
        qty = rng.choice(QUANTITIES[inst])
        if c == "behavior":
            out.append("behavior " + rng.choices([b.value for b in FakeBehavior], [3, 6, 1, 1, 1, 2, 2])[0])
        elif c in ("entry", "increase", "reduce", "exit", "op-reduce", "op-exit"):
            out.append(f"{c} {inst} {qty}")
        elif c in ("cancel", "modify"):
            out.append(f"{c} {rng.randrange(5)}")
        elif c == "fill":
            out.append(f"fill {rng.randrange(5)}" + ("" if rng.random() < 0.5 else f" {qty}"))
        elif c == "shuffle":
            out.append(f"shuffle {rng.randrange(10_000)}")
        elif c in ("query-fail", "query-delay"):
            out.append(f"{c} {rng.randrange(4)}")
        elif c in ("disconnect", "raise-after-send", "kill-file", "ioc"):
            out.append(f"{c} {rng.choice(['on', 'off'])}")
        elif c == "reply":
            out.append(f"reply {rng.choice(REPLIES)}")
        elif c == "liquidity":
            out.append(f"liquidity {inst} {rng.choice(['all', '0', qty])}")
        elif c == "stale":
            out.append(f"stale {inst} {rng.choice(['on', 'off'])}")
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
