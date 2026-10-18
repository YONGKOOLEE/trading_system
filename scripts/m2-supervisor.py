"""M2 감독 프로세스 (CP-037 D-223, T-054) — **M2 PAPER(바이낸스 Spot 테스트넷) 전용**. LIVE·BACKTEST·M1은 거부.

작업 스케줄러(로그온 트리거 + 매일 보조 트리거, 등록은 사용자가 scripts/schedule-register-m2.ps1로 — 자동 실행 등록 승인 D-231
뒤)가 pythonw로 실행한다. 사람이 직접 실행해도 같다. 본체는 trading_system.app.m2_supervisor. 이 스크립트는 시작 일치 표의
테스트넷 접속 도메인과 시작 점검용 바이낸스 세션 함수를 넘기기만 한다(app은 바이낸스 패키지를 import하지 않는다, D-119).

사용 (PowerShell, M2 체크아웃에서):
    cd C:\\AI\\trading_system\\m2-binance
    C:\\AI\\trading_system\\trading_system\\.venv\\Scripts\\python.exe scripts\\m2-supervisor.py --mode PAPER --market M2
감독 정지(run은 정지 요청 파일로 정상 종료 — 강제 종료 없음):
    C:\\AI\\trading_system\\trading_system\\.venv\\Scripts\\python.exe scripts\\m2-supervisor.py --mode PAPER --market M2 --stop

종료 코드: 0 정상(감독 정지 요청·의도된 run 종료·이미 실행 중) | 2 run 시작 전 거부 | 3 처리하지 못한 예외(모듈 적재 실패 포함).
사유는 var/paper/logs/m2-supervisor-YYYYMMDD.log(UTC 날짜). 설정 거부는 표준 출력에만(파일을 만들지 않음, V-296).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _fatal(what: str = "모듈 적재 실패") -> int:
    """모듈 적재 실패·진입 단계 예외 — pythonw에는 표준 오류가 없으므로 파일로 남기고 코드 3."""
    import traceback
    from datetime import datetime
    try:
        logs = ROOT / "var" / "paper" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "m2-supervisor-fatal.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()}\tM2 감독 {what} (코드 3)\n{traceback.format_exc()}\n")
    except OSError:
        pass
    return 3


if __name__ == "__main__":
    try:
        from trading_system.app.m2_supervisor import m2_supervisor_main
        from trading_system.broker.binance.preflight import open_testnet_session
        from trading_system.broker.binance.run_start import M2RunConnector
    except Exception:  # noqa: BLE001
        sys.exit(_fatal())
    try:
        sys.exit(m2_supervisor_main(hosts=M2RunConnector.hosts, open_session=open_testnet_session))
    except SystemExit:
        raise
    except BaseException:  # noqa: BLE001 — 진입 단계(감독 본체 밖)의 예외도 코드 3으로
        sys.exit(_fatal("진입 단계 예외"))
