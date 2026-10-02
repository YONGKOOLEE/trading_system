"""watchdog 실행 본체 (03 D-054, 04 V-111). 본 프로세스와 별도로 사람이 실행한다(scripts/watchdog.py).

app 패키지에 두는 이유: 설정(모드 검사)·저장소·감시 모듈을 함께 쓰는 조립 지점이기 때문 (03 §7).
"""

from __future__ import annotations

from collections.abc import Sequence

from trading_system.config.errors import ConfigError
from trading_system.domain.clock import SystemClock
from trading_system.ops.cli import _parser, _resolve
from trading_system.storage.repository import ModeMismatch, Repository, StorageFailure


def watchdog_main(argv: Sequence[str] | None = None) -> int:
    """본 프로세스 하트비트 감시 (03 D-054). 멈추면 알림만 한다 — 자동 재시작·자동 청산 없음."""
    import time as _time
    from datetime import UTC, datetime

    from trading_system.monitoring.liveness import watchdog_check
    from trading_system.storage.alerts import AuditAlertSink
    p = _parser("watchdog")
    p.add_argument("--once", action="store_true", help="한 번만 확인하고 종료")
    p.add_argument("--interval", type=int, default=10)
    ns = p.parse_args(argv)
    try:
        mode, db, _ = _resolve(ns)
    except ConfigError as exc:
        print(f"거부: {exc}")
        return 2
    hb = db.parent / "heartbeat"
    fallback = db.parent / "logs" / "alerts-fallback.log"
    alerted = False
    while True:
        st = watchdog_check(hb, datetime.now(UTC))
        print(f"{datetime.now(UTC).isoformat()} {'정상' if st.alive else '이상'}: {st.detail}")
        if not st.alive and not alerted:
            try:
                repo = Repository(db, mode)
                try:
                    AuditAlertSink(repo, SystemClock(), fallback, actor="watchdog").alert(
                        "EMERGENCY", f"watchdog: {st.detail}", None)
                finally:
                    repo.close()
            except (StorageFailure, ModeMismatch, OSError):
                fallback.parent.mkdir(parents=True, exist_ok=True)
                with fallback.open("a", encoding="utf-8") as f:
                    f.write("\t".join((datetime.now(UTC).isoformat(), "EMERGENCY", "-", f"watchdog: {st.detail}"))
                            + "\n")
            alerted = True
        if st.alive:
            alerted = False
        if ns.once:
            return 0 if st.alive else 1
        _time.sleep(ns.interval)
