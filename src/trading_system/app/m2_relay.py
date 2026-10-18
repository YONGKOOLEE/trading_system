"""M2 PAPER run의 다른 스레드 → 판정·실행 스레드 연결 (CP-036 D-215, T-048).

바이낸스 쪽 부품(어댑터 경보·사용자 스트림 상태·시세 공급 K10)은 엔진보다 먼저 만들어진다(엔진이 어댑터를 받으므로).
이 중계는 엔진이 붙기 전 들어온 일을 모아 두었다가 attach 때 순서대로 넘기고, 그 뒤로는 모두 engine.call_soon으로
판정·실행 스레드에서 처리한다 — 정지·알림 기록 순서를 한 스레드로 모은다(D-215, broker_alerts.py "저장소 동시 사용" 미결 해소).

- 어댑터·클라이언트 경보(on_alert(kind, message)): K5(418 IP 차단)·K6(-1021 연속 3회) → BrokerAlertHalts(정지),
  그 밖(CLOCK·FILTER·ORDER_BLOCKED·FILL_CONFLICT·FEED·RULES) → 경고 알림.
- 사용자 스트림 상태(on_stream_state(state, message)) — M1 ws_state_handler와 같은 규칙(K5 실시간 경로):
  OFFLINE → K5 발동(actor realtime) + 실시간 경로 끊김 보고. ONLINE·RECONNECTED → 실시간 경로 정상 보고 + 대조 요청
  (STREAM_RECONNECT, D-109). REFUSED(구독 거부) → K5 + 긴급 알림. BAD_MESSAGE → 경고 알림.
- 정지 제어(halts: activate·report_condition) — 시세 공급의 K10(데이터 품질).
문구에는 키·서명·응답 원문이 없다(보내는 쪽 규칙). 이 모듈은 바이낸스 패키지를 import하지 않는다(D-119).
"""

from __future__ import annotations

import threading
from collections.abc import Callable
from typing import TYPE_CHECKING

from trading_system.domain.enums import HaltType
from trading_system.halt.broker_alerts import BrokerAlertHalts
from trading_system.halt.manager import K5_LINK_REALTIME

if TYPE_CHECKING:
    from .engine import Engine


class M2Relay:
    def __init__(self, account: str) -> None:
        self._account = account
        self._engine: Engine | None = None
        self._pending: list[Callable[[], None]] = []
        self._lock = threading.Lock()
        self._bridge: BrokerAlertHalts | None = None
        self.on_ip_ban_cleared: Callable[[], None] = lambda: None
        # [CP-037 D-226] 사용자 스트림 구독 연결 상태 — K8 자동 재개 추가 조건(app/m2_resume_conditions). 상태를 받는
        # 즉시(스트림 스레드에서) 바꾼다. 처음은 False(연결 확인 전에는 자동 재개하지 않음). bool 대입 하나라 잠금 없음
        self.stream_online = False

    # --- 연결 ---
    def attach(self, engine: "Engine") -> None:
        # 경보는 call_soon 안(판정·실행 스레드)에서 다리로 넘기므로 정지 관리자를 직접 쓴다
        self._bridge = BrokerAlertHalts(engine.halts, engine.repo, engine.clock, engine.alerts,  # type: ignore[arg-type]
                                        on_ip_ban_cleared=lambda: self.on_ip_ban_cleared())
        with self._lock:
            self._engine = engine
            pending, self._pending = self._pending, []
        for fn in pending:
            engine.call_soon(fn)

    def _soon(self, fn: Callable[[], None]) -> None:
        with self._lock:
            if self._engine is None:
                self._pending.append(fn)
                return
            engine = self._engine
        engine.call_soon(fn)

    def _e(self) -> "Engine":
        assert self._engine is not None
        return self._engine

    # --- 경보 (어댑터·클라이언트·공급) ---
    def alert(self, kind: str, message: str) -> None:
        def run() -> None:
            if kind in (HaltType.K5.value, HaltType.K6.value) and self._bridge is not None:
                self._bridge(kind, message)
            else:
                self._e().alerts.alert("WARNING", f"바이낸스 {kind}: {message}", None)
        self._soon(run)

    # --- 사용자 스트림 상태 ---
    def stream_state(self, state: str, message: str) -> None:
        acct = self._account
        if state in ("ONLINE", "RECONNECTED"):
            self.stream_online = True
        elif state in ("OFFLINE", "REFUSED"):
            self.stream_online = False

        def run() -> None:
            e = self._e()
            if state in ("OFFLINE", "REFUSED"):
                e.halts.activate(HaltType.K5, "realtime", f"사용자 스트림 {state}: {message}", acct)
                e.halts.report_condition(HaltType.K5, acct, K5_LINK_REALTIME, False, f"사용자 스트림 {state}")
                if state == "REFUSED":
                    e.alerts.alert("EMERGENCY", f"사용자 스트림 구독 거부 — 키·권한 확인 필요 (PV-M2-06): {message}", None)
            elif state in ("ONLINE", "RECONNECTED"):
                e.halts.report_condition(HaltType.K5, acct, K5_LINK_REALTIME, True, f"사용자 스트림 {state}")
                e.request_reconcile()
            else:
                e.alerts.alert("WARNING", f"사용자 스트림 {state}: {message}", None)
        self._soon(run)

    # --- 정지 제어 (시세 공급 K10) ---
    def activate(self, halt_type: HaltType, actor: str, reason: str, account: str | None = None,
                 **kw: object) -> str:
        self._soon(lambda: self._e().halts.activate(halt_type, actor, reason, account, **kw))
        return ""

    def report_condition(self, halt_type: HaltType, account: str | None, condition: str, ok: bool,
                         detail: str = "", **kw: object) -> None:
        self._soon(lambda: self._e().halts.report_condition(halt_type, account, condition, ok, detail, **kw))
