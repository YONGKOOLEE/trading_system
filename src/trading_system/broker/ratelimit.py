"""호출 제한기 (03 D-048, 04 V-71). 여러 스레드(판정·실행, 시세 IO)가 같은 브로커 계정을 나눠 쓴다.

- API(TR)마다 최소 간격(모의: TR당 1초 1회 → 기본 1.1초 여유). 선택적으로 그룹(예: 실전 조회 그룹) 간격.
- 우선순위: 결과 불명 해소 > 대조 > 운영 요청 > 시세. 같은 TR(또는 같은 그룹)을 기다리는 호출 중 높은 순위가 먼저,
  같은 순위는 먼저 온 순서.
- 우선순위는 호출 문맥으로 정한다(`call_priority`). 어댑터 인터페이스(fake와 공통)를 바꾸지 않기 위함.
- 1700~1702(호출 제한 초과) 수신: 해당 TR을 지수 대기(1·2·4…최대 30초). 성공하면 초기화 (K-U10 보수값).
- 대기 상한을 넘기면 AcquireTimeout — 주문이면 전송 전 실패, 조회면 조회 실패로 처리한다.
"""

from __future__ import annotations

import contextlib
import contextvars
import itertools
import threading
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from enum import IntEnum


class Priority(IntEnum):
    RESOLVE = 0     # 결과 불명 해소 조회
    ORDER = 1       # 주문 전송 (TR이 조회와 달라 실제 경합은 없음)
    RECONCILE = 2
    OPS = 3
    MARKET = 4


_current: contextvars.ContextVar[Priority] = contextvars.ContextVar("broker_priority", default=Priority.OPS)


@contextlib.contextmanager
def call_priority(p: Priority) -> Iterator[None]:
    token = _current.set(p)
    try:
        yield
    finally:
        _current.reset(token)


def current_priority() -> Priority:
    return _current.get()


class AcquireTimeout(RuntimeError):
    pass


@dataclass(order=True)
class _Waiter:
    priority: int
    seq: int
    tr: str = field(compare=False)


class RateLimiter:
    def __init__(self, per_tr_interval: float = 1.1, groups: dict[str, tuple[set[str], float]] | None = None,
                 monotonic: Callable[[], float] = time.monotonic, max_penalty: float = 30.0) -> None:
        self._interval = per_tr_interval
        self._groups = groups or {}   # 이름 → (TR 집합, 그룹 최소 간격)
        self._mono = monotonic
        self._max_penalty = max_penalty
        self._cond = threading.Condition()
        self._next_tr: dict[str, float] = {}
        self._next_group: dict[str, float] = {}
        self._penalty: dict[str, float] = {}
        self._waiting: list[_Waiter] = []
        self._seq = itertools.count()
        self.grants: list[tuple[str, int]] = []  # (TR, 우선순위) 허가 순서 — 시험·감시용 (최근 1000건)

    def _group_of(self, tr: str) -> str | None:
        return next((g for g, (trs, _) in self._groups.items() if tr in trs), None)

    def _ready_at(self, tr: str) -> float:
        t = self._next_tr.get(tr, 0.0)
        g = self._group_of(tr)
        return max(t, self._next_group.get(g, 0.0)) if g else t

    def _shares(self, a: str, b: str) -> bool:
        if a == b:
            return True
        ga = self._group_of(a)
        return ga is not None and ga == self._group_of(b)

    def acquire(self, tr: str, timeout: float = 30.0, priority: Priority | None = None) -> None:
        pr = current_priority() if priority is None else priority
        me = _Waiter(int(pr), next(self._seq), tr)
        deadline = self._mono() + timeout
        with self._cond:
            self._waiting.append(me)
            try:
                while True:
                    now = self._mono()
                    ahead = [w for w in self._waiting if w < me and self._shares(w.tr, tr)]
                    ready = self._ready_at(tr)
                    if not ahead and now >= ready:
                        break
                    if now >= deadline:
                        raise AcquireTimeout(f"{tr}: 호출 제한 대기 {timeout}초 초과")
                    wait = min(deadline - now, max(ready - now, 0.0) if not ahead else 0.05)
                    self._cond.wait(max(wait, 0.001))
                now = self._mono()
                self._next_tr[tr] = now + self._interval
                g = self._group_of(tr)
                if g:
                    self._next_group[g] = now + self._groups[g][1]
                self.grants.append((tr, int(pr)))
                del self.grants[:-1000]
            finally:
                self._waiting.remove(me)
                self._cond.notify_all()

    def penalize(self, tr: str) -> float:
        """호출 제한 초과 응답. 다음 허가까지 대기를 늘린다. 적용한 대기(초)를 돌려준다."""
        with self._cond:
            p = min(self._penalty.get(tr, 0.5) * 2, self._max_penalty)
            self._penalty[tr] = p
            self._next_tr[tr] = max(self._next_tr.get(tr, 0.0), self._mono() + p)
            self._cond.notify_all()
            return p

    def wake(self) -> None:
        """대기 중인 호출을 깨워 다시 판정하게 한다(시계 주입 시험·종료 처리용)."""
        with self._cond:
            self._cond.notify_all()

    def succeeded(self, tr: str) -> None:
        with self._cond:
            self._penalty.pop(tr, None)
