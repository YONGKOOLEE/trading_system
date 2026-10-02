"""사람이 실행하는 로컬 운영 도구의 본체 (03 §6.5·§6.6·§13, D-051). `scripts/`의 파일은 이 함수를 부른다.

- kill: KILL 파일을 **먼저** 만들고(저장소 장애에도 동작) 명령 테이블에 K1 발동을 기록한다.
- resume: 명령 테이블에 재개 요청을 기록한다. 실제 재개 판정은 본 프로세스가 한다 (재개 조건·원격 여부).
  KILL 파일은 `--clear-kill-file`을 줄 때만 지운다 (로컬에서만, 03 §6.5).
- status: 해제되지 않은 정지·KILL 파일·대기 명령·결과 불명 의도 수를 보여 준다 (비밀 없음).
로컬 인증은 Windows 로그인 사용자 = 운영자 (단일 사용자 가정, 03 §6.6).
"""

from __future__ import annotations

import argparse
import getpass
import uuid
from collections.abc import Sequence
from pathlib import Path

from trading_system.config.errors import ConfigError
from trading_system.config.modes import parse_trade_mode
from trading_system.domain.clock import SystemClock
from trading_system.domain.enums import HaltType, IntentState, Mode
from trading_system.storage.repository import ModeMismatch, Repository, StorageFailure


REPO_ROOT = Path(__file__).resolve().parents[3]  # src/trading_system/ops/cli.py → 저장소 루트


def default_paths(root: Path, mode: Mode) -> tuple[Path, Path]:
    base = root / "var" / mode.value.lower()
    return base / "trading.db", base / "KILL"


def _parser(prog: str) -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog=prog)
    p.add_argument("--mode", required=True, help="BACKTEST|PAPER|LIVE (대문자 정확히)")
    p.add_argument("--root", default=str(REPO_ROOT), help="저장소 루트 (기본: 이 도구가 있는 저장소, 현재 폴더 아님)")
    p.add_argument("--db", help="DB 경로 (기본: var/<mode>/trading.db)")
    p.add_argument("--kill-file", help="KILL 파일 경로 (기본: var/<mode>/KILL)")
    p.add_argument("--account", help="계좌 별칭 (없으면 전체)")
    return p


def _resolve(ns: argparse.Namespace) -> tuple[Mode, Path, Path]:
    mode = parse_trade_mode(ns.mode)
    db, kill = default_paths(Path(ns.root), mode)
    db = Path(ns.db) if ns.db else db
    kill = Path(ns.kill_file) if ns.kill_file else kill
    return mode, db.resolve(), kill.resolve()


def _record(db: Path, mode: Mode, command: str, args: dict) -> str:
    repo = Repository(db, mode)
    try:
        cid = f"local-{uuid.uuid4().hex}"
        with repo.tx():
            now = SystemClock().now()
            repo.insert_command(cid, "LOCAL", getpass.getuser(), command, args, now)
            repo.audit(now, "OPS_COMMAND_RECEIVED", "local", cid, {"command": command})
        return cid
    finally:
        repo.close()


def kill_main(argv: Sequence[str] | None = None) -> int:
    p = _parser("kill")
    p.add_argument("--reason", default="로컬 킬 스위치")
    ns = p.parse_args(argv)
    try:
        mode, db, kill = _resolve(ns)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    kill.parent.mkdir(parents=True, exist_ok=True)
    kill.write_text("KILL\n", encoding="utf-8")  # 저장소와 독립 (D-051)
    print(f"KILL 파일 생성: {kill}")
    try:
        cid = _record(db, mode, "KILL", {"account": ns.account, "reason": ns.reason})
    except (StorageFailure, ModeMismatch) as exc:
        print(f"명령 기록 실패(저장소): {exc}. KILL 파일로 위험 확대 전송은 막혀 있음")
        return 1
    print(f"K1 발동 명령 기록: {cid}")
    return 0


def resume_main(argv: Sequence[str] | None = None) -> int:
    p = _parser("resume")
    p.add_argument("halt_type", help="K1~K11")
    p.add_argument("--clear-kill-file", action="store_true", help="K1 재개 시 KILL 파일도 지움")
    ns = p.parse_args(argv)
    try:
        mode, db, kill = _resolve(ns)
        halt_type = HaltType(ns.halt_type)
    except (ConfigError, ValueError) as exc:
        print(f"거부: {exc}")
        return 2
    try:
        cid = _record(db, mode, "RESUME", {"account": ns.account, "halt_type": halt_type.value})
    except (StorageFailure, ModeMismatch) as exc:
        print(f"명령 기록 실패(저장소): {exc}")
        return 1
    print(f"재개 요청 기록: {cid} (판정은 본 프로세스, 결과는 status로 확인)")
    if halt_type is HaltType.K1 and kill.exists():
        if ns.clear_kill_file and _k1_released(db, mode):
            kill.unlink()
            print(f"KILL 파일 삭제: {kill}")
        elif ns.clear_kill_file:
            # 재개 판정 전에 지우면 유일한 차단 수단이 먼저 사라진다 (CP-018 R-11)
            print("거부: K1이 해제된 기록이 없어 KILL 파일을 지우지 않음. 본 프로세스가 재개를 처리한 뒤 다시 실행")
        else:
            print(f"주의: KILL 파일이 남아 있어 위험 확대 전송은 계속 막힘 ({kill}). 지우려면 --clear-kill-file")
    return 0


def _k1_released(db: Path, mode: Mode) -> bool:
    """가장 최근 K1이 해제되었고 해제되지 않은 K1이 없는지."""
    try:
        repo = Repository(db, mode)
    except (ModeMismatch, StorageFailure):
        return False
    try:
        latest = repo.latest_halt(HaltType.K1.value)
        return latest is not None and latest["state"] == "RELEASED" and "K1" not in {
            h["halt_type"] for h in repo.halt_rows(None)}
    finally:
        repo.close()


def status_main(argv: Sequence[str] | None = None) -> int:
    ns = _parser("status").parse_args(argv)
    try:
        mode, db, kill = _resolve(ns)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    print(f"모드: {mode.value}")
    print(f"KILL 파일: {'있음' if kill.exists() else '없음'} ({kill})")
    if not db.exists():
        print(f"DB 없음: {db}")
        return 0
    try:
        repo = Repository(db, mode)
    except ModeMismatch as exc:
        print(f"거부: {exc}")
        return 2
    try:
        halts = repo.halt_rows(ns.account)
        print(f"해제되지 않은 정지: {len(halts)}건")
        for h in halts:
            print(f"  {h['halt_type']} {h['state']} 계좌={h['account'] or '전체'} 종목={h['scope_instrument'] or '-'} "
                  f"전략={h['scope_owner'] or '-'} 발동={h['created_at']} 사유={h['reason']}")
        pending = repo.pending_commands()
        print(f"처리 대기 명령: {len(pending)}건")
        unknown = repo.intents_in_state(IntentState.UNKNOWN)
        print(f"결과 불명 의도: {len(unknown)}건")
        for i in unknown:
            print(f"  {i.intent_id} {i.instrument} {i.side} {i.quantity}")
    finally:
        repo.close()
    return 0
