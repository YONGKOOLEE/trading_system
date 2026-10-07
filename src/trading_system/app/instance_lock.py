"""단일 실행 잠금 (RV-15): 같은 모드의 엔진은 하나만 돈다. `var/<mode>/run.lock`의 첫 바이트를 프로세스 수명 동안
잠근다(Windows msvcrt.locking, 그 밖은 fcntl.flock). 프로세스가 죽으면 OS가 잠금을 푼다(남은 파일은 무해).

preflight 등 다른 도구는 `instance_running(base)`로 실행 중인지 확인한다(잠깐 잡았다 바로 놓음).
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import IO

LOCK_NAME = "run.lock"
AUTOPILOT_LOCK = "autopilot.lock"   # PAPER 무인 자동 운영(app/autopilot.py)의 단일 실행 (CP-029)


class InstanceLocked(RuntimeError):
    pass


def _lock(f: IO[bytes]) -> None:
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    else:  # pragma: no cover — 확인된 환경은 Windows (CLAUDE.md §5)
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(f: IO[bytes]) -> None:
    f.seek(0)
    if os.name == "nt":
        import msvcrt
        msvcrt.locking(f.fileno(), msvcrt.LK_UNLCK, 1)
    else:  # pragma: no cover
        import fcntl
        fcntl.flock(f.fileno(), fcntl.LOCK_UN)


class InstanceLock:
    def __init__(self, path: Path, f: IO[bytes]) -> None:
        self.path = path
        self._f: IO[bytes] | None = f

    def release(self) -> None:
        if self._f is None:
            return
        try:
            _unlock(self._f)
        except OSError:
            pass
        finally:
            self._f.close()
            self._f = None


def acquire_instance_lock(base: Path, name: str = LOCK_NAME) -> InstanceLock:
    """잠금을 잡는다. 다른 프로세스(또는 같은 프로세스의 다른 핸들)가 잡고 있으면 InstanceLocked.
    name: 잠금 파일 이름 (기본 run.lock = 엔진, autopilot.lock = 자동 운영)."""
    base.mkdir(parents=True, exist_ok=True)
    path = base / name
    f = open(path, "a+b")  # noqa: SIM115 — 프로세스 수명 동안 열어 둔다
    try:
        _lock(f)
    except OSError as exc:
        f.close()
        raise InstanceLocked(f"같은 모드의 다른 실행이 있음 ({path}) — 이중 실행 거부") from exc
    return InstanceLock(path, f)


def instance_running(base: Path) -> bool:
    """다른 실행이 잠금을 쥐고 있으면 True. 잠금 파일이 없으면 False(실행된 적 없음)."""
    if not (base / LOCK_NAME).exists():
        return False
    try:
        acquire_instance_lock(base).release()
    except InstanceLocked:
        return True
    return False


__all__ = ["InstanceLock", "InstanceLocked", "acquire_instance_lock", "instance_running", "LOCK_NAME", "AUTOPILOT_LOCK"]
