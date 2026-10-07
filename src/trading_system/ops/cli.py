"""사람이 실행하는 로컬 운영 도구의 본체 (03 §6.5·§6.6·§13, D-051). `scripts/`의 파일은 이 함수를 부른다.

- kill: KILL 파일을 **먼저** 만들고(저장소 장애에도 동작) 명령 테이블에 K1 발동을 기록한다.
- resume: 명령 테이블에 재개 요청을 기록한다. 실제 재개 판정은 본 프로세스가 한다 (재개 조건·원격 여부).
  KILL 파일은 `--clear-kill-file`을 줄 때만 지운다 (로컬에서만, 03 §6.5).
- status: 해제되지 않은 정지·KILL 파일·대기 명령·결과 불명 의도, 포지션·미체결·당일 의도/체결 수, 최근 운영 명령·ALERT
  10건, 하트비트 나이(본 프로세스 생존), 마지막 시계(SNTP) 점검, 전략 상태를 보여 준다 (읽기 전용, 비밀 없음, RV-16).
로컬 인증은 Windows 로그인 사용자 = 운영자 (단일 사용자 가정, 03 §6.6).
"""

from __future__ import annotations

import argparse
import getpass
import json
import uuid
from collections.abc import Sequence
from datetime import datetime, time
from pathlib import Path

from trading_system.config.errors import ConfigError
from trading_system.config.modes import parse_trade_mode
from trading_system.domain.clock import KST, Clock, SystemClock, to_iso
from trading_system.domain.enums import HaltType, IntentState, Mode
from trading_system.storage.jsonlog import Masker
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


def reconcile_resolve_main(argv: Sequence[str] | None = None) -> int:
    """불일치 항목 해소 또는 재대조 요청을 기록한다 (03 §6.3). 판정·반영은 본 프로세스가 한다."""
    p = _parser("reconcile-resolve")
    p.add_argument("item_id", nargs="?", help="불일치 항목 ID (status로 확인)")
    p.add_argument("method", nargs="?", choices=["EXTERNAL", "APPLY_FILL", "CLOSE_ORDER", "BROKER_ACTION"])
    p.add_argument("--run", action="store_true", help="재대조 요청 (--account 필요)")
    ns = p.parse_args(argv)
    try:
        mode, db, _ = _resolve(ns)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    if ns.run:
        if not ns.account:
            print("거부: --run에는 --account가 필요")
            return 2
        command, args = "RECONCILE", {"account": ns.account}
    elif ns.item_id and ns.method:
        command, args = "RECONCILE_RESOLVE", {"item_id": ns.item_id, "method": ns.method}
    else:
        print("거부: 항목 ID와 해소 방법, 또는 --run")
        return 2
    try:
        cid = _record(db, mode, command, args)
    except (StorageFailure, ModeMismatch) as exc:
        print(f"명령 기록 실패(저장소): {exc}")
        return 1
    print(f"{command} 요청 기록: {cid} (결과는 status로 확인)")
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


def status_main(argv: Sequence[str] | None = None, *, clock: Clock | None = None) -> int:
    ns = _parser("status").parse_args(argv)
    now = (clock or SystemClock()).now()
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
                  f"전략={h['scope_owner'] or '-'} 발동={_kst(h['created_at'])} 사유={h['reason']}")
        pending = repo.pending_commands()
        print(f"처리 대기 명령: {len(pending)}건")
        items = repo.open_recon_items(ns.account)
        print(f"열린 대조 불일치: {len(items)}건")
        for it in items:
            print(f"  {it['item_id']} {it['kind']} 종목={it['instrument'] or '-'} 내부={it['internal_value'] or '-'} "
                  f"브로커={it['broker_value'] or '-'}")
        unknown = repo.intents_in_state(IntentState.UNKNOWN)
        print(f"결과 불명 의도: {len(unknown)}건")
        for i in unknown:
            print(f"  {i.intent_id} {i.instrument} {i.side} {i.quantity}")
        _print_operations(repo, ns.account, now)
    finally:
        repo.close()
    _print_heartbeat(db.parent / "heartbeat", now)
    return 0


RECENT = 10
_MASK = Masker()  # 화면 출력 전 가림 (계좌번호 형태 등, R-11)
HEARTBEAT_STALE_SECONDS = 30  # monitoring.liveness.HEARTBEAT_STALE와 같은 값 (ops는 monitoring을 import하지 않는다)


def _acct(sql: str, account: str | None, params: tuple = ()) -> tuple[str, tuple]:
    return (sql + " AND account=?", params + (account,)) if account else (sql, params)


def _print_operations(repo: Repository, account: str | None, now: datetime) -> None:
    """RV-16: 운영 중 판단에 필요한 상태 (읽기 전용 SELECT만)."""
    q = repo.select
    sql, prm = _acct("SELECT account, instrument, quantity, avg_price FROM position WHERE CAST(quantity AS REAL) > 0",
                     account)
    pos = q(sql + " ORDER BY account, instrument", prm)
    print(f"보유 포지션: {len(pos)}건")
    for p in pos:
        print(f"  {p['account']} {p['instrument']} 수량 {p['quantity']} 평균가 {p['avg_price']}")
    # 취소 주문 행(INTERNAL_CANCEL)은 수량 0으로 ACCEPTED에 남으므로 미체결로 세지 않는다 (리허설 결함)
    sql, prm = _acct("SELECT * FROM broker_order WHERE state IN ('ACCEPTED','PARTIAL') "
                     "AND origin <> 'INTERNAL_CANCEL'", account)
    orders = q(sql + " ORDER BY created_at", prm)
    print(f"미체결 주문: {len(orders)}건")
    for o in orders:
        print(f"  주문번호 {o['order_no']} {o['instrument']} {o['side']} {o['filled_quantity']}/{o['quantity']} "
              f"{o['state']} {o['origin']} 접수 {_kst(o['created_at'])}")
    day_start = to_iso(datetime.combine(now.astimezone(KST).date(), time(0), tzinfo=KST))
    sql, prm = _acct("SELECT COUNT(*) AS n FROM intent WHERE created_at>=?", account, (day_start,))
    n_intent = q(sql, prm)[0]["n"]
    sql = ("SELECT COUNT(*) AS n FROM fill f JOIN broker_order o ON o.order_key=f.order_key WHERE f.at>=?"
           + (" AND o.account=?" if account else ""))
    n_fill = q(sql, (day_start, account) if account else (day_start,))[0]["n"]
    print(f"당일 의도 {n_intent}건, 체결 {n_fill}건 (KST 0시부터)")
    cmds = q("SELECT * FROM ops_command ORDER BY created_seq DESC LIMIT ?", (RECENT,))
    print(f"최근 운영 명령 {len(cmds)}건 (최신 먼저):")
    for c in cmds:
        print(_MASK.text(f"  {_kst(c['created_at'])} {c['channel']} {c['command']} {_short(c['args'])} → "
                         f"{c['status']} 결과={c['result'] or '-'}"))
    alerts = q("SELECT at, actor, ref, summary FROM audit_event WHERE kind='ALERT' ORDER BY seq DESC LIMIT ?",
               (RECENT,))
    print(f"최근 알림 {len(alerts)}건 (최신 먼저):")
    for a in alerts:
        try:
            body = json.loads(a["summary"])
        except ValueError:
            body = {}
        print(_MASK.text(f"  {_kst(a['at'])} {body.get('level', '?')} {body.get('message', '')}"
                         f"{' (' + a['ref'] + ')' if a['ref'] else ''}"))
    cc = q("SELECT at, source, offset_seconds, result FROM clock_check ORDER BY seq DESC LIMIT 1")
    if cc:
        c = cc[0]
        print(f"마지막 시계 점검: {_kst(c['at'])} {c['result']} 오차 {c['offset_seconds'] or '-'}초 ({c['source']})")
    else:
        print("마지막 시계 점검: 기록 없음")
    states = repo.strategy_states()
    print(f"전략 상태: {len(states)}건")
    for st in states:
        print(f"  {st['strategy_id']} v{st['version']} {st['market']} {st['state']}"
              f"{' 정지사유=' + st['stop_reason'] if st['stop_reason'] else ''} 갱신 {_kst(st['updated_at'])}")


def _print_heartbeat(path: Path, now: datetime) -> None:
    try:
        beat = datetime.fromisoformat(path.read_text(encoding="utf-8").strip())
    except FileNotFoundError:
        print("하트비트: 없음 — 본 프로세스가 시작되지 않았거나 멈춤")
        return
    except (OSError, ValueError):
        print("하트비트: 읽을 수 없음")
        return
    if beat.tzinfo is None:
        print("하트비트: 시간대 없는 시각 — 판정 불가")
        return
    age = (now - beat).total_seconds()
    state = "살아 있음" if age <= HEARTBEAT_STALE_SECONDS else "멈춤 의심"
    print(f"하트비트: {age:.0f}초 전 ({_kst(beat.isoformat())}) — 본 프로세스 {state}")


def _kst(text: str | None) -> str:
    if not text:
        return "-"
    try:
        return datetime.fromisoformat(text).astimezone(KST).strftime("%m-%d %H:%M:%S")
    except ValueError:
        return text


def _short(args: str | None, limit: int = 80) -> str:
    s = args or ""
    return s if len(s) <= limit else s[:limit] + "…"


# --- 저장소 운영 (T-011, 03 §9·D-089) ---------------------------------------------------------

def backup_main(argv: Sequence[str] | None = None) -> int:
    from trading_system.storage.maintenance import MaintenanceError, backup
    p = _parser("backup")
    p.add_argument("--keep", type=int, default=7)
    ns = p.parse_args(argv)
    try:
        mode, db, _ = _resolve(ns)
        target = backup(db, db.parent / "backup", ns.keep)
    except (ConfigError, MaintenanceError) as exc:
        print(f"실패: {exc}")
        return 1
    print(f"백업 완료: {target}")
    return 0


def restore_test_main(argv: Sequence[str] | None = None) -> int:
    from trading_system.storage.maintenance import restore_test
    p = _parser("restore-test")
    p.add_argument("--backup", help="백업 파일 (기본: 가장 최근 백업)")
    ns = p.parse_args(argv)
    try:
        mode, db, _ = _resolve(ns)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    if ns.backup:
        target = Path(ns.backup)
    else:
        backups = sorted((db.parent / "backup").glob("trading-*.db"))
        if not backups:
            print("백업 없음")
            return 1
        target = backups[-1]
    rep = restore_test(target, mode.value)
    print(f"복원 시험: {target.name} → {'통과' if rep.ok else '실패'}")
    for k, v in rep.checks.items():
        print(f"  {k}: {v}")
    return 0 if rep.ok else 1


def archive_logs_main(argv: Sequence[str] | None = None) -> int:
    from datetime import UTC, datetime

    from trading_system.storage.jsonlog import archive_logs
    p = _parser("archive-logs")
    p.add_argument("--days", type=int, default=90, help="보존 기간(일, 01 §2.12)")
    ns = p.parse_args(argv)
    try:
        mode, db, _ = _resolve(ns)
        done = archive_logs(db.parent / "logs", ns.days, datetime.now(UTC).date())
    except (ConfigError, RuntimeError, OSError) as exc:
        print(f"실패: {exc}")
        return 1
    print(f"보관 후 삭제한 로그: {len(done)}개 (감사·주문 기록은 삭제하지 않음)")
    return 0


def migrate_main(argv: Sequence[str] | None = None) -> int:
    from trading_system.storage.maintenance import MaintenanceError, migrate
    ns = _parser("migrate").parse_args(argv)
    try:
        mode, db, _ = _resolve(ns)
        applied = migrate(db, db.parent / "backup")
    except (ConfigError, MaintenanceError) as exc:
        print(f"실패: {exc}")
        return 1
    print(f"적용한 버전: {applied or '없음(최신)'}")
    return 0

