"""브로커 어댑터 경보 → 정지 (CP-024 D-113·D-115, 04 V-145(418)·V-146(-1021), T-M2-09).

어댑터(broker/binance/client.py)는 정지 관리자를 모른다(의존 방향 03 §7). 어댑터 생성자의 on_alert(kind, message) 자리에
이 객체를 넣으면 어댑터가 판정한 경보를 정지로 바꾼다.
- "K5"(HTTP 418 IP 차단): 즉시 K5(계좌 범위 없음 — IP 차단은 이 프로그램의 모든 호출을 막는다) + 긴급 알림. 연결 경로는
  K5_LINK_IP_BAN이며 자동 재개하지 않는다: 대조 일치·경로 구분 없는 연결 보고로는 풀리지 않고, 사람이
  confirm_ip_ban_cleared로 차단 해제를 확인(호출 예산 차단 해제 포함)한 **뒤의** 대조 일치로만 해제된다(D-113 "사람 재개").
- "K6"(-1021 서명 요청 연속 3회, 어댑터가 판정): K6 발동 = 시계 판정 요청(D-115). 해제는 시계 점검의 clock_ok 보고
  (K6 자동 해제 유형 그대로, 01 K6). [제안] M2 시계 판정에 바이낸스 서버 시각 오차(0.5초 경고, A-064)를 넣을지는 T-M2-14.
- 그 밖의 경보(CLOCK 시계 경고 등): 경고 알림만.
스레드: K5·K6은 매수 미체결 취소 유형이 아니라(CANCELS_OPEN_BUYS 아님) 발동이 주문을 내지 않는다. 어댑터 호출 스레드에서
불러도 주문 송신 스레드 제약(D-038)에 걸리지 않는다. 저장소 동시 사용 방식은 M2 엔진 조립(T-M2-14)에서 정한다.
"""

from __future__ import annotations

from collections.abc import Callable

from trading_system.domain.clock import Clock
from trading_system.domain.enums import HaltType
from trading_system.domain.sinks import AlertSink
from trading_system.storage.repository import Repository

from .manager import K5_IP_BAN_ACTOR, K5_LINK_IP_BAN, HaltManager

K6_CLOCK_ACTOR = "broker_clock"
IP_BAN_CLEARED = "IP_BAN_CLEARED"   # 감사 기록 종류 — 사람이 IP 차단 해제를 확인


class BrokerAlertHalts:
    def __init__(self, halts: HaltManager, repo: Repository, clock: Clock, alerts: AlertSink, *,
                 on_ip_ban_cleared: Callable[[], None]) -> None:
        self._halts = halts
        self._repo = repo
        self._clock = clock
        self._alerts = alerts
        self._on_cleared = on_ip_ban_cleared

    def __call__(self, kind: str, message: str) -> None:
        if kind == HaltType.K5.value:
            self._halts.activate(HaltType.K5, K5_IP_BAN_ACTOR, message, None)
        elif kind == HaltType.K6.value:
            self._halts.activate(HaltType.K6, K6_CLOCK_ACTOR, message, None)
        else:
            self._alerts.alert("WARNING", f"브로커 경보 {kind}: {message}", None)

    def confirm_ip_ban_cleared(self, actor: str) -> None:
        """사람이 IP 차단 해제를 확인했을 때만 부른다(운영 절차, D-113). 자동 호출 금지.

        호출 예산의 차단을 풀고 K5의 IP 차단 경로를 정상으로 보고한다. K5 해제는 그 뒤의 대조 일치가 한다."""
        self._on_cleared()
        with self._repo.tx():
            self._repo.audit(self._clock.now(), IP_BAN_CLEARED, actor, None, {"link": K5_LINK_IP_BAN})
        self._halts.report_condition(HaltType.K5, None, K5_LINK_IP_BAN, True, f"사람 확인: {actor}")
