"""같은 조건의 반복 경보 억제 — 시세 공급 조회 실패·사용자 스트림 해석 불가 메시지 (CP-036 재검토, DP-67).

공급 스레드는 5초마다 시세를 조회하므로 장애가 1시간 이어지면 실패 경보가 수백 건 나가 텔레그램이 넘친다. 저장 쪽
AuditAlertSink(DP-33)는 같은 문구를 60초 창으로 묶지만, 여기 문구는 오류 내용·예산 잔여에 따라 달라 묶이지 않는다.
그래서 보내는 쪽에서 **조건 키**(예: "ticker BTCUSDT")로 묶는다.

규칙 [제안 값]
- 조건 시작: 바로 1건(원래 문구).
- 계속: 직전 알림 뒤 SUMMARY_EVERY_S(30분)가 지난 다음 실패 때 요약 1건(계속 시간·억제 건수). 그 사이는 세기만.
- 회복: 성공이 RECOVER_AFTER_S(5분) 동안 이어지면 1건(지속 시간·억제 건수). 그 전에 다시 실패하면 같은 일로 본다
  (깜빡임이 시작·회복 알림을 반복하지 않게).
- 회복으로 끝난 뒤 다시 실패하면 새 일 — 바로 알린다. 회복 판정에 5분 안정이 필요하므로 깜빡여도 키 하나에
  시작·회복 알림은 5분에 1쌍을 넘지 않는다.
정지·게이트 판단은 이 억제와 무관하다 — 시세가 늙으면 게이트 G11이 위험 확대를 거부한다(경보는 사람에게 알리는 것뿐).
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

SUMMARY_EVERY_S = 1800.0
RECOVER_AFTER_S = 300.0


@dataclass
class _Cond:
    kind: str
    since: float
    is_open: bool = True
    emitted: bool = False          # 이번 일의 시작을 알렸는가
    suppressed: int = 0            # 직전 알림 뒤 센 건수
    last_emit: float | None = None
    ok_since: float | None = None


class ConditionAlerts:
    def __init__(self, emit: Callable[[str, str], None], now_s: Callable[[], float], *,
                 every_s: float = SUMMARY_EVERY_S, recover_after_s: float = RECOVER_AFTER_S) -> None:
        self._emit = emit
        self._now = now_s
        self._every = every_s
        self._recover = recover_after_s
        self._conds: dict[str, _Cond] = {}

    def __repr__(self) -> str:
        return f"ConditionAlerts(open={sorted(k for k, c in self._conds.items() if c.is_open)})"

    def is_open(self, key: str) -> bool:
        c = self._conds.get(key)
        return c is not None and c.is_open

    def fail(self, key: str, kind: str, message: str) -> bool:
        """조건 발생 1건. 알렸으면 True."""
        t = self._now()
        c = self._conds.get(key)
        if c is None:
            c = self._conds[key] = _Cond(kind, t)
        elif not c.is_open:
            c.is_open, c.since, c.emitted, c.kind = True, t, False, kind
        c.ok_since = None
        if not c.emitted:
            self._send(c, kind, message, t)                    # 새 일의 시작은 바로 알린다
            return True
        if c.last_emit is not None and t - c.last_emit < self._every:
            c.suppressed += 1
            return False
        self._send(c, kind, f"{message} — {_mins(t - c.since)} 계속, 직전 알림 뒤 같은 조건 {c.suppressed}건 억제", t)
        return True

    def ok(self, key: str, label: str) -> bool:
        """조건 정상 1건. 회복을 알렸으면 True."""
        c = self._conds.get(key)
        if c is None or not c.is_open:
            return False
        t = self._now()
        if c.ok_since is None:
            c.ok_since = t
        if t - c.ok_since < self._recover:
            return False
        c.is_open = False
        self._send(c, c.kind, f"{label} 회복 — {_mins(c.ok_since - c.since)} 지속, 직전 알림 뒤 같은 조건 "
                              f"{c.suppressed}건 억제", t)
        return True

    def _send(self, c: _Cond, kind: str, text: str, t: float) -> None:
        c.last_emit, c.suppressed, c.emitted = t, 0, True
        try:
            self._emit(kind, text)
        except Exception:  # noqa: BLE001 — 알림 전달 실패가 공급·수신 스레드를 죽이지 않게
            pass


def _mins(seconds: float) -> str:
    return f"{max(0.0, seconds) / 60:.0f}분"
