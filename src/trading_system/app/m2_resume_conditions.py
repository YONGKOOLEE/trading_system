"""M2 K8 자동 재개의 추가 조건 (CP-037 D-226, T-054) — run(판정·실행 스레드)이 AUTO 채널 RESUME K8을 처리할 때 본다.

D-226: K8 자동 재개 M2판 = D-152 기준(채널 AUTO, 명령 시점 새 대조 + 01 §2.11 조건 1~5) **+ M2 추가 조건**:
  ① 바이낸스 서버 시각 오차 < 0.5초(A-064, 설정 server_time_warn_ms) — 명령 처리 시점에 새로 잰다(조회 1회, weight 1).
  ② exchangeInfo 규칙·rateLimits 적재 — 유니버스 심볼마다 규칙이 있어야 한다. 공급 스레드는 규칙 재적재가 실패하면
     (조회·해석·rateLimits 없음) 규칙을 비운다(broker/binance/feed `_load_rules` → clear_rules) → 규칙 있음 = 둘 다 적재.
  ③ 사용자 스트림 구독 연결 상태 — M2Relay.stream_online(ONLINE·RECONNECTED 뒤 True, OFFLINE·REFUSED 뒤 False,
     처음에는 False — 연결 확인 전에는 재개하지 않는다).
  ④ 시세 공급 정상 — 유니버스 심볼마다 시세가 있고 신선도(설정 quote_max_age_seconds, A-064 60초) 이내.
하나라도 어긋나면 거부 사유(문자열)를 돌려준다 — 처리기(ops/commands.OpsProcessor)는 새 대조 없이 REJECTED로 기록한다.
조건 1~5(대조 일치·결과 불명 없음 등)는 기존 경로(새 대조 → 복구 판정)가 그대로 본다.

원격(/resume K8)·로컬 재개에는 쓰지 않는다(CP-026 조건 그대로). 이 모듈은 바이낸스 패키지를 import하지 않는다(D-119) —
서버 시각 측정 함수는 조립 지점(app/main._run_m2)이 연결 묶음(M2Run.server_offset_now)으로 넣는다.
"""

from __future__ import annotations

from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Protocol

from trading_system.domain.market import Quote
from trading_system.domain.trading_rules import SymbolRules


class _Facts(Protocol):
    def rules(self, instrument: str) -> SymbolRules | None: ...

    def quote(self, instrument: str) -> Quote | None: ...


@dataclass
class M2ResumeConditions:
    server_offset_ms: Callable[[], int]
    warn_ms: int
    facts: _Facts
    symbols: Sequence[str]
    stream_online: Callable[[], bool]
    now: Callable[[], datetime]
    max_quote_age: timedelta

    def problems(self) -> str | None:
        """None = M2 추가 조건 충족. 아니면 사유(모든 미충족 항목, 비밀값·응답 원문 없음)."""
        out: list[str] = []
        missing = [s for s in self.symbols if self.facts.rules(s) is None]
        if missing:
            out.append(f"exchangeInfo 규칙·rateLimits 미적재: {', '.join(missing)}")
        if not self.stream_online():
            out.append("사용자 스트림 미연결")
        now = self.now()
        for s in self.symbols:
            q = self.facts.quote(s)
            if q is None:
                out.append(f"시세 없음: {s}")
                continue
            age = now - q.at
            if age > self.max_quote_age:
                out.append(f"시세 {int(age.total_seconds())}초 지남(기준 {int(self.max_quote_age.total_seconds())}초): {s}")
        try:
            offset = self.server_offset_ms()
        except Exception as exc:  # noqa: BLE001 — 조회 실패는 조건 미충족(시각을 모르면 재개하지 않는다)
            out.append(f"바이낸스 서버 시각 조회 실패({type(exc).__name__})")
        else:
            if abs(offset) >= self.warn_ms:
                out.append(f"바이낸스 서버 시각 오차 {offset}ms ≥ 기준 {self.warn_ms}ms (A-064)")
        if not out:
            return None
        return "M2 자동 재개 조건 미충족(D-226): " + "; ".join(out)


__all__ = ["M2ResumeConditions"]
