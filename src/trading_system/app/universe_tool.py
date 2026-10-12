"""U-B 사람 목록 제출 도구 (CP-034 D-198, A-092 Q-8, T-041). 사람이 직접 실행한다. 본체는 이 모듈, `scripts/universe.py`가 부른다.

- submit: 목록 파일(`code[,memo]` 한 줄에 하나, UTF-8·BOM 허용, KRX 6자리 영숫자)을 검증해 모드 저장소에 새 버전으로
  추가한다. 형식·중복·모르는 종목(배포 설정 `[[instruments]]`에 없음)·계좌 감시 종목 상한(모든 전략 목록 합집합 + 보유
  ≤ 15) 중 하나라도 어기면 **제출 전체를 거부하고 이전 버전을 유지**한다(거부도 감사 기록). 내용 해시가 직전 버전과
  같으면 새 버전을 만들지 않는다.
- show: 목록 버전 이력(번호·제출 시각·채널·해시·종목 수).
- 로컬 도구만: 원격(텔레그램)·대화 경로는 없다(목록 추가는 위험 확대 입력, R-21 원격 범위 밖). 제출 채널은 LOCAL.
- BACKTEST·PAPER만. LIVE는 거부한다. 브로커에 연결하지 않는다. 비밀 파일을 읽지 않는다.
- 적용: 판정 시각 T에는 T − 10분 이전에 제출 완료된 최신 버전만 쓴다 — 판정 직전 제출은 다음 판정부터.
"""

from __future__ import annotations

import argparse
import getpass
from collections.abc import Sequence
from pathlib import Path

from trading_system.config.app_file import load_app_settings
from trading_system.config.errors import ConfigError
from trading_system.config.modes import parse_trade_mode
from trading_system.domain.clock import KST, SystemClock
from trading_system.domain.enums import Mode
from trading_system.domain.universe import (
    APPLY_LAG, MAX_WATCHED, SUBMIT_CHANNEL, content_hash, parse_list_file, valid_list_id, watched_set,
)
from trading_system.storage.repository import Repository

from .wiring import load_strategy

REPO_ROOT = Path(__file__).resolve().parents[3]


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="scripts/universe.py")
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("submit", help="목록 파일을 새 버전으로 제출")
    s.add_argument("--mode", required=True, help="PAPER|BACKTEST")
    s.add_argument("--list", required=True, dest="list_id", help="목록 ID (전략 명세 universe_list)")
    s.add_argument("--file", required=True, help="목록 파일 (code[,memo], UTF-8)")
    s.add_argument("--root", default=None)
    v = sub.add_parser("show", help="목록 버전 이력")
    v.add_argument("--mode", required=True, help="PAPER|BACKTEST")
    v.add_argument("--list", default=None, dest="list_id")
    v.add_argument("--root", default=None)
    return p


def universe_main(argv: Sequence[str] | None = None, root: Path | None = None) -> int:
    ns = _parser().parse_args(argv)
    try:
        mode = parse_trade_mode(ns.mode)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    if mode not in (Mode.BACKTEST, Mode.PAPER):
        print(f"거부: {mode.value}는 이 도구에서 다루지 않는다 (실전 경로 없음)")
        return 2
    if ns.list_id is not None and not valid_list_id(ns.list_id):
        print("거부: 목록 ID 형식 (영문·숫자·-·_, 64자 이하)")
        return 2
    base = Path(ns.root) if ns.root else (root or REPO_ROOT)
    db = base / "var" / mode.value.lower() / "trading.db"
    if ns.cmd == "show":
        if not db.exists():
            print(f"저장소 없음: {db}")
            return 1
        repo = Repository(db, mode)
        try:
            return _show(repo, ns.list_id)
        finally:
            repo.close()
    return _submit(base, db, mode, ns.list_id, Path(ns.file))


def _submit(base: Path, db: Path, mode: Mode, list_id: str, file: Path) -> int:
    try:
        data = file.read_bytes()
    except OSError as exc:
        print(f"제출 거부: 목록 파일을 읽을 수 없음 ({type(exc).__name__})")
        return 1
    entries, problems = parse_list_file(data)
    codes = [e.code for e in entries]
    account, known, other_lists, fixed = None, set(), [], []
    try:
        app = load_app_settings(base / "config" / f"app.{mode.value.lower()}.toml", mode, base)
        account, known = app.account, set(app.instruments)
        for dep in app.strategies:
            spec = load_strategy(dep)[0].spec
            if spec.universe_method == "U-B" and spec.universe_list and spec.universe_list != list_id:
                other_lists.append(spec.universe_list)
            elif spec.universe_method == "U-A":
                fixed.append(tuple(spec.universe))
    except ConfigError as exc:
        problems.append(f"배포 설정을 읽을 수 없어 종목·감시 상한을 검사할 수 없음: {exc}")
    if account is not None:
        unknown = [c for c in codes if c not in known]
        if unknown:
            problems.append(f"모르는 종목(배포 설정 [[instruments]]에 없음) {len(unknown)}개: {', '.join(unknown[:10])}")
    db.parent.mkdir(parents=True, exist_ok=True)
    repo = Repository(db, mode)
    try:
        watched: set[str] = set()
        if account is not None:
            latest = [vs[-1].codes for lid in sorted(set(other_lists)) if (vs := repo.universe_versions(lid))]
            held = [k for k, p in repo.positions(account).items() if p.quantity > 0]
            watched = watched_set([*fixed, *latest, codes], held)
            if len(watched) > MAX_WATCHED:
                problems.append(f"계좌 감시 종목(모든 전략 목록 합집합 + 보유) {len(watched)}개 > 상한 {MAX_WATCHED} "
                                "(D-198 (6), A-092 Q-8)")
        actor = getpass.getuser()
        now = SystemClock().now()
        prior = repo.universe_versions(list_id)
        if problems:
            with repo.tx():
                repo.audit(now, "UNIVERSE_SUBMIT_REJECTED", f"universe-tool:{actor}", list_id,
                           {"list": list_id, "mode": mode.value, "problems": problems[:20],
                            "kept_version": prior[-1].version if prior else None})
            print(f"제출 거부: 목록 {list_id} — 이전 버전 유지"
                  f"({'버전 ' + str(prior[-1].version) if prior else '버전 없음'})")
            for p in problems[:20]:
                print(f"  - {p}")
            return 1
        digest = content_hash(entries)
        if prior and prior[-1].content_hash == digest:
            print(f"변경 없음: 목록 {list_id} 버전 {prior[-1].version}과 내용이 같음 — 새 버전을 만들지 않음")
            return 0
        with repo.tx():
            version = repo.insert_universe_version(list_id, entries, digest, now, SUBMIT_CHANNEL, actor)
            repo.audit(now, "UNIVERSE_SUBMITTED", f"universe-tool:{actor}", list_id,
                       {"list": list_id, "version": version, "hash": digest, "count": len(entries),
                        "channel": SUBMIT_CHANNEL, "mode": mode.value, "watched": len(watched)})
        lag = int(APPLY_LAG.total_seconds() // 60)
        print(f"제출 완료: 목록 {list_id} 버전 {version} (종목 {len(entries)}개, 해시 {digest[:12]}, "
              f"제출 {now.astimezone(KST):%Y-%m-%d %H:%M:%S} KST)")
        print(f"  적용: 판정 시각 T에는 T − {lag}분 이전 제출분만 — {(now + APPLY_LAG).astimezone(KST):%H:%M} 이후 판정부터")
        if not entries:
            print("  빈 목록: 이 목록을 쓰는 전략은 신규 진입 없음, 보유 종목 청산 판정은 계속 (D-198 (5))")
        return 0
    finally:
        repo.close()


def _show(repo: Repository, list_id: str | None) -> int:
    ids = [list_id] if list_id else repo.universe_list_ids()
    if not ids:
        print("제출된 목록 없음")
    for lid in ids:
        versions = repo.universe_versions(lid)
        print(f"목록 {lid}: 버전 {len(versions)}개")
        for v in versions:
            print(f"  v{v.version} {v.submitted_at.astimezone(KST):%Y-%m-%d %H:%M:%S} KST {v.channel} "
                  f"해시 {v.content_hash[:12]} 종목 {len(v.codes)}개")
    return 0
