"""1회 정정 도구: 수정 전 EXTERNAL 해소가 남긴 유령 미체결 외부 주문을 FILLED로 고친다 (A-109 ④). 사람이 직접 실행한다.

배경: 2026-10-16 PV-11 run에서 이미 체결된 외부 주문을 EXTERNAL로 인정했더니 ACCEPTED 0/1로 기록돼 status에 미체결로 보였다.
해소 경로는 고쳤고(브로커 상태로 기록), 이미 기록된 행은 이 도구로 한 건씩 고친다.

- `--mode PAPER|BACKTEST`만 (LIVE 거부, 저장소를 열기 전). DB가 없으면 거부(만들지 않음).
- 같은 디렉터리의 엔진 실행 잠금(run.lock)·자동 운영 잠금(autopilot.lock)이 잡혀 있으면 거부. 쓰는 동안 run.lock을 잡는다.
- 근거: 그 주문의 EXTERNAL 해소 항목(감사 RECONCILE_RESOLVE)의 브로커 값이 같은 수량의 FILLED (ops.reconcile).
- 브로커 연결 없음. 체결 행·포지션·소유권 변경 없음. 감사 EXTERNAL_ORDER_CORRECTED 1건.
"""

from __future__ import annotations

import argparse
import getpass
from collections.abc import Sequence
from pathlib import Path

from trading_system.app.instance_lock import AUTOPILOT_LOCK, InstanceLocked, acquire_instance_lock, instance_running
from trading_system.config.errors import ConfigError
from trading_system.domain.clock import SystemClock
from trading_system.domain.enums import Mode
from trading_system.ops.cli import LIVE_REFUSAL, _header, _open_repo, _parser, _resolve, _scope
from trading_system.ops.reconcile import ResolveError, correct_external_filled
from trading_system.storage.repository import ModeMismatch, SchemaMismatch, StorageFailure


def fix_external_main(argv: Sequence[str] | None = None) -> int:
    p = _parser("fix-external-order")
    p.description = __doc__
    p.add_argument("order_no", help="브로커 주문번호 (status의 '미체결 주문' 줄, origin EXTERNAL)")
    ns = p.parse_args(argv)
    try:
        mode, db, _ = _resolve(ns)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    if mode is Mode.LIVE:
        print(LIVE_REFUSAL.format(tool="fix-external-order"))
        return 2
    if not db.exists():
        print(f"거부: DB 없음: {db}")
        return 2
    base = db.parent
    if instance_running(base, AUTOPILOT_LOCK):
        print(f"거부: 자동 운영이 실행 중(autopilot.lock 보유, {base}) — 끝난 뒤 다시 실행")
        return 2
    try:
        lock = acquire_instance_lock(base)   # run이 돌고 있으면 거부, 쓰는 동안 run 시작도 막는다
    except InstanceLocked:
        print(f"거부: 엔진이 실행 중(run.lock 보유, {base}) — run을 끈 뒤 다시 실행")
        return 2
    try:
        return _fix(ns, mode, db)
    finally:
        lock.release()


def _fix(ns: argparse.Namespace, mode: Mode, db: Path) -> int:
    _header(ns)
    try:
        repo = _open_repo(db, mode, _scope(ns, mode))
    except (ModeMismatch, SchemaMismatch, StorageFailure) as exc:
        print(f"거부: {exc}")
        return 2
    try:
        with repo.tx():
            note = correct_external_filled(repo, ns.order_no, ns.account, getpass.getuser(), SystemClock().now())
    except ResolveError as exc:
        print(f"거부: {exc}")
        return 2
    finally:
        repo.close()
    print(f"정정 완료: {note}")
    print("감사 기록 EXTERNAL_ORDER_CORRECTED 1건. status로 '미체결 주문'에서 빠졌는지 확인")
    return 0
