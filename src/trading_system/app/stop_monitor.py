"""M2 손절 감시 (CP-040 D-262·D-263, T-067, 승인 A-115 Q-2 (나) — 시스템 1분 닫힌 봉 감시 → 중앙 경로 시장가 청산).

판정·실행 스레드의 엔진 단계 하나("stop" — 대조·분봉 저장 다음, 예정 판정 전)로만 돈다. 감시는 브로커를 부르지 않는다:
청산 요청(GateRequest) → 시장 프로파일 prepare(M2 주문 유형·가격 D-104·D-120, dust D-102) → 게이트(Gate.decide) →
승인이면 실행기(Executor.submit) → 어댑터. 주문 경로는 하나다(CLAUDE.md §8-2).

- 대상 (D-263 (1)): CAP-HLD-02를 선언한 전략이 **소유한**(R-28 — 소유권 소유자 = 그 전략) 열린 포지션. 청산 가능 수량
  (판정 입력과 같은 보유 표시, dust 제외)이 없으면 감시하지 않는다. 비소유(수동·다른 전략) 보유는 보지 않는다.
- 손절가: 이 소유 기간의 체결된 진입·증액 의도 가운데 손절가가 있는 최신 값(domain/stop_watch.latest_stop_price). 없으면
  청산하지 않고 경고 1회.
- 시각·데이터 (D-263 (2)): 1분 격자(PeriodGrid(1) — 주문 창 안, 확정 여유) 시각 at마다 직전 닫힌 1분 봉 하나를 저장 봉에서
  읽는다(판정과 같은 출처 — M2MinuteInput.closed_bar). 그 시각 조회가 끝나지 않았으면 기다림 상한(fetch_wait)까지 기다리고,
  넘으면 FETCH_TIMEOUT. 한 봉은 포지션마다 한 번만 판단한다(다음 분 = 새 판단). 진행 중 분은 보지 않는다.
- 판단 불가 (D-263 (3)): 결측·품질·저장 실패·조회 시한 → **청산하지 않는다**(§8-10) + 감사 NO_DATA, 같은 포지션 3회 연속이면
  경고 1건 → 계속되면 30분마다 요약 → 다시 판단되면 회복 INFO (DP-73 방식).
- 발동 = 닫힌 봉 종가 ≤ 손절가. 요청 = 원천 SIGNAL(그 전략의 청산 신호와 같은 요청 유형·같은 정지 규칙, D-263 (4) "새 예외
  없음"), 요청 유형 EXIT, 규칙 ID CAP-HLD-02, 신호 ID = 소유권·감지 분, 기한 = 감시 시각 + 1분. 감사 STOP_MONITOR에 원천
  STOP_MONITOR.
- 요청하지 않음 (보낼 수 없거나 불확실):
  · 같은 종목 결과 불명 의도 → 해소 전 새 요청 0 (§8-6, R-06). 같은 종목 처리 중 의도(매수·매도) → 종결 뒤 다음 닫힌 봉에서
    다시 판단 (D-263 (5), 주문 변경 자동 재시도 금지 §8-7).
  · 01 §2.6 정지 표(domain/halts.halt_rejection — 게이트 G2와 같은 함수)가 그 청산 신호를 막으면 게이트에 보내지 않는다(같은
    결과, 판정 행 매분 누적 방지). K8(재시작 직후 포함)·K1·K5~K7은 여기서 0, K2는 허용(위험 축소, §8-3).
  · 전략 상태가 청산 신호를 받지 않음.
  → 감사 SKIPPED + 사유가 바뀔 때마다 경고 1건.
- 연속 미체결·거부 (D-263 (5)): 손절 요청이 체결 0으로 종결(EXPIRED·거부·미전송 등)되거나 게이트·주문 조건에서 거부되면 그
  포지션 연속 수 +1, 체결이 있으면 0. 3의 배수마다 긴급 알림. 상태는 메모리(재시작하면 0 — 임시 판단).
- 감시 단절(프로세스 사망·재부팅) 동안 보호 없음, 재시작 뒤 K8 재개 전까지도 없음(D-263 (8)). 주문 창(UTC 00:10~23:50) 밖
  20분은 분봉 조회가 없어(공급 격자 = 주문 창, T-051) 감시하지 않는다 — 남은 위험 (임시 판단).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import TYPE_CHECKING

from trading_system.domain.enums import GateResult, HaltType, OrderType, RequestSource, RequestType, Side
from trading_system.domain.halts import HaltView, RequestView, halt_rejection
from trading_system.domain.minute_series import ONE_MINUTE, PeriodGrid, last_closed_start
from trading_system.domain.models import GateRequest
from trading_system.domain.stop_watch import (
    STOP_CAPABILITY, STOP_ORIGIN, STOP_RULE_ID, latest_stop_price, stop_idem_key, stop_triggered,
)
from trading_system.storage.repository import OwnershipRow

from .m2_minutes import M2MinuteInput

if TYPE_CHECKING:
    from trading_system.domain.strategy import PositionView

    from .engine import Engine, LoadedStrategy
    from .market_profile import SpotProfile

STOP_AUDIT = "STOP_MONITOR"
UNJUDGED_ALERT_AFTER = 3                      # D-263 (3) "3회 연속이면 경고" (DP-73 방식)
UNJUDGED_SUMMARY_EVERY = timedelta(minutes=30)
FAILURE_ALERT_EVERY = 3                       # D-263 (5) "연속 미체결·거부 3회면 긴급 알림" — 3의 배수마다 (임시 판단)


@dataclass
class _Unjudged:
    count: int = 0
    alerted_at: datetime | None = None
    suppressed: int = 0


def stop_strategies(strategies: list[LoadedStrategy]) -> list[LoadedStrategy]:
    return [ls for ls in strategies if STOP_CAPABILITY in ls.strategy.spec.capabilities]


class StopMonitor:
    def __init__(self, engine: Engine, profile: SpotProfile, minutes: M2MinuteInput,
                 strategies: list[LoadedStrategy]) -> None:
        self.engine = engine
        self.profile = profile
        self.minutes = minutes
        self.strategies = stop_strategies(strategies)
        self.grid = PeriodGrid(1, minutes.margin)
        self._judged: dict[str, datetime] = {}          # 소유권 ID → 마지막으로 판단한 봉 시작
        self._pending: dict[str, tuple[str, str]] = {}  # 손절 청산 의도 ID → (소유권 ID, 종목)
        self._failures: dict[str, int] = {}             # 소유권 ID → 연속 미체결·거부 수
        self._unjudged: dict[str, _Unjudged] = {}       # 소유권 ID → 연속 판단 불가
        self._skip_warned: dict[str, str] = {}          # 소유권 ID → 마지막으로 경고한 요청 안 함 사유
        self._no_stop_warned: set[str] = set()

    def __repr__(self) -> str:
        return f"StopMonitor(strategies={[ls.owner for ls in self.strategies]}, grid=1m)"

    # --- 한 tick ---------------------------------------------------------------------------------------------
    def step(self, now: datetime) -> None:
        self._poll_outcomes()
        at = self.grid.latest_at_or_before(now)
        if at is None:   # 주문 창 밖 — 분봉 조회 격자 밖이라 판단할 봉이 없다 (남은 위험)
            return
        bar_start = last_closed_start(at, self.minutes.margin)
        e = self.engine
        for ls in self.strategies:
            for inst in self.profile._instruments(ls):   # noqa: SLF001 — 판정 입력과 같은 범위 (U-A ∩ 유니버스)
                own = e.repo.active_ownership(e.account, inst)
                if own is None or own.owner != ls.owner:
                    continue   # 비소유(수동·다른 전략) 보유는 감시하지 않는다 (R-28)
                pos = self.profile.sellable_position(inst, ls.owner)
                if pos is None or pos.quantity <= 0 or not pos.owned_by_me:
                    continue
                if self._judged.get(own.ownership_id) == bar_start:
                    continue
                unfetched = bool(self.minutes.pending_fetch([inst], at))
                if unfetched and now - at <= self.grid.fetch_wait:
                    continue   # 그 시각 조회 대기 — 다음 tick에 다시
                self._judged[own.ownership_id] = bar_start
                self._judge(ls, inst, own, pos, at, bar_start, unfetched)

    def _judge(self, ls: LoadedStrategy, inst: str, own: OwnershipRow, pos: PositionView, at: datetime,
               bar_start: datetime, unfetched: bool) -> None:
        e = self.engine
        oid = own.ownership_id
        base = {"origin": STOP_ORIGIN, "owner": ls.owner, "instrument": inst, "ownership": oid,
                "bar_start": bar_start.isoformat(), "at": at.isoformat()}
        stop = self._stop_price(own, inst, ls.owner)
        if stop is None:
            if oid not in self._no_stop_warned:
                self._no_stop_warned.add(oid)
                e.alerts.alert("WARNING", f"손절 감시 {inst} ({ls.owner}): 손절가 없음 — 감시·청산하지 않음 "
                                          "(체결된 진입·증액 신호에 손절가 없음, D-263 (1))", oid)
            return
        status, bar = self.minutes.closed_bar(e.repo, inst, at, unfetched)
        if bar is None:
            self._audit(base | {"action": "NO_DATA", "status": status, "stop_price": str(stop)}, oid)
            self._unjudged_failed(oid, inst, status, at)
            return
        self._unjudged_ok(oid, inst)
        if not stop_triggered(bar.close, stop):
            self._skip_warned.pop(oid, None)
            return
        key = stop_idem_key(oid, bar_start)
        detail = base | {"close": str(bar.close), "stop_price": str(stop), "quantity": str(pos.quantity)}
        reason = self._blocked(inst, ls.owner)
        if reason is not None:
            self._audit(detail | {"action": "SKIPPED", "reason": reason}, key)
            if self._skip_warned.get(oid) != reason:
                self._skip_warned[oid] = reason
                e.alerts.alert("WARNING", f"손절 감시 {inst} ({ls.owner}): 손절가 도달(닫힌 봉 {bar_start:%H:%M} 종가 "
                                          f"{bar.close} ≤ {stop}) — 청산 요청 안 함: {reason}", key)
            return
        self._skip_warned.pop(oid, None)
        req = GateRequest(source=RequestSource.SIGNAL, request_type=RequestType.EXIT, account=e.account,
                          instrument=inst, side=Side.SELL, quantity=pos.quantity, order_type=OrderType.MARKET,
                          price=bar.close, idem_key=key, owner=ls.owner, valid_until=at + ONE_MINUTE,
                          signal_id=key, rule_id=STOP_RULE_ID)
        self._audit(detail | {"action": "REQUESTED"}, key)
        terms = self.profile.prepare(req)   # M2 주문 유형·가격 (시세 정상 = 시장가, 지연 = IOC 지정가), dust 선별
        if terms is None:
            self._failed(oid, inst, ls.owner, "주문 유형·가격을 정할 수 없음 또는 확실한 dust")
            return
        d = e.gate.decide(terms)
        if d.result is GateResult.APPROVED and d.approved is not None:
            self._pending[d.approved.intent_id] = (oid, inst)
            e.executor.submit(d.approved)
        else:
            self._failed(oid, inst, ls.owner, f"게이트 거부 {d.failed_check}: {d.reason}")

    # --- 요청 가능 여부 ---------------------------------------------------------------------------------------
    def _blocked(self, inst: str, owner: str) -> str | None:
        e = self.engine
        repo = e.repo
        if repo.unknown_intents(e.account, inst):
            return "같은 종목 결과 불명 의도 — 해소 전 새 요청 없음 (§8-6, R-06)"
        if repo.active_new_intents(e.account, inst, Side.SELL.value) or \
                repo.active_new_intents(e.account, inst, Side.BUY.value):
            return "같은 종목 처리 중인 주문 의도 — 종결 뒤 다음 닫힌 봉에서 다시 판단 (D-263 (5), §8-7)"
        view = RequestView(RequestSource.SIGNAL, RequestType.EXIT, Side.SELL, inst, owner)
        for row in repo.halt_rows(e.account):
            h = HaltView(HaltType(row["halt_type"]), row["scope_instrument"], row["scope_owner"], row["variant"])
            why = halt_rejection(h, view)
            if why:
                return f"{why} (01 §2.6 — 청산 신호와 같은 규칙)"
        why = e.registry.signal_rejection(owner, RequestType.EXIT)
        if why is not None:
            return f"전략 상태: {why}"
        return None

    def _stop_price(self, own: OwnershipRow, inst: str, owner: str) -> Decimal | None:
        """이 소유 기간(소유권을 만든 진입 의도부터)의 진입·증액 의도, 최신 순 → latest_stop_price."""
        e = self.engine
        if own.intent_id is None:
            return None
        rows = e.repo.select(
            "SELECT filled_quantity, stop_price FROM intent WHERE account=? AND instrument=? AND owner=? "
            "AND request_type IN ('ENTRY','INCREASE') AND created_seq >= "
            "(SELECT created_seq FROM intent WHERE intent_id=?) ORDER BY created_seq DESC",
            (e.account, inst, owner, own.intent_id))
        return latest_stop_price((Decimal(r["filled_quantity"]),
                                  Decimal(r["stop_price"]) if r["stop_price"] is not None else None) for r in rows)

    # --- 결과 추적·알림 ---------------------------------------------------------------------------------------
    def _poll_outcomes(self) -> None:
        repo = self.engine.repo
        for iid, (oid, inst) in list(self._pending.items()):
            intent = repo.intent(iid)
            if not intent.state.is_terminal:
                continue
            del self._pending[iid]
            if intent.filled_quantity > 0:
                self._failures.pop(oid, None)
            else:
                self._failed(oid, inst, intent.owner or "", f"손절 청산 {intent.state.value} (체결 0)")

    def _failed(self, oid: str, inst: str, owner: str, why: str) -> None:
        n = self._failures.get(oid, 0) + 1
        self._failures[oid] = n
        if n % FAILURE_ALERT_EVERY == 0:
            self.engine.alerts.alert("EMERGENCY", f"손절 감시 {inst} ({owner}): 손절 청산 연속 미체결·거부 {n}회 — 최근 "
                                                  f"{why}. 보유가 손절가 아래에 남아 있을 수 있음, 사람 확인 (D-263 (5))",
                                     oid)

    def _unjudged_failed(self, oid: str, inst: str, status: str, now: datetime) -> None:
        st = self._unjudged.setdefault(oid, _Unjudged())
        st.count += 1
        if st.count < UNJUDGED_ALERT_AFTER:
            return
        if st.alerted_at is None:
            text = (f"손절 감시 판정 불가 {inst}: 직전 닫힌 1분 봉 {status} — {st.count}회 연속, 그 분은 청산하지 않음 "
                    "(불확실하면 주문하지 않음, D-263 (3))")
        elif now - st.alerted_at >= UNJUDGED_SUMMARY_EVERY:
            text = (f"손절 감시 판정 불가 계속 {inst}: 최근 {status} — {st.count}회 연속 (직전 알림 뒤 묶은 "
                    f"{st.suppressed}건, D-263 (3)·DP-73)")
        else:
            st.suppressed += 1
            return
        st.alerted_at, st.suppressed = now, 0
        self.engine.alerts.alert("WARNING", text, oid)

    def _unjudged_ok(self, oid: str, inst: str) -> None:
        st = self._unjudged.pop(oid, None)
        if st is not None and st.alerted_at is not None:
            self.engine.alerts.alert("INFO", f"손절 감시 회복 {inst}: 닫힌 1분 봉 다시 판단 ({st.count}회 연속 판단 불가 뒤, "
                                             f"묶은 알림 {st.suppressed}건)", oid)

    def _audit(self, summary: dict, ref: str) -> None:
        e = self.engine
        with e.repo.tx():
            e.repo.audit(e.clock.now(), STOP_AUDIT, "engine", ref, summary)
