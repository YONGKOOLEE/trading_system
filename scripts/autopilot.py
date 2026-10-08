"""PAPER 무인 자동 운영 (T-025, CP-029 draft, A-078). 작업 스케줄러가 평일 06:45에 실행한다(등록은 사용자가
scripts/schedule-register.ps1로). 사람이 직접 실행해도 같다. **PAPER 전용 — LIVE·BACKTEST는 거부.** 본체는
trading_system.app.autopilot.

사용 예: .venv/Scripts/python scripts/autopilot.py --mode PAPER --help

종료 코드 (A-084): 0 정상 | 1 하루를 돌았으나 문제 있음(일일 요약의 "확인 필요") | 2 run 시작 전 거부·중단 |
3 run 시작 뒤 중단 또는 처리되지 않은 예외(traceback은 일 로그에). 사유는 var/paper/logs/autopilot-YYYYMMDD.log
(거부 사유 포함). 모듈 적재 자체가 실패하면 var/paper/logs/autopilot-fatal.log에 남기고 코드 3.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def _import_failed() -> int:
    """모듈 적재 실패 — pythonw에는 표준 오류가 없으므로 파일로 남기고 코드 3 (1은 "문제 있음"과 겹친다)."""
    import traceback
    from datetime import datetime
    try:
        logs = ROOT / "var" / "paper" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "autopilot-fatal.log").open("a", encoding="utf-8") as f:
            f.write(f"{datetime.now().isoformat()}\t자동 운영 모듈 적재 실패 (코드 3)\n{traceback.format_exc()}\n")
    except OSError:
        pass
    return 3


if __name__ == "__main__":
    try:
        from trading_system.app.autopilot import autopilot_main
    except Exception:  # noqa: BLE001
        sys.exit(_import_failed())
    sys.exit(autopilot_main())   # 처리되지 않은 예외는 autopilot_main 안에서 코드 3으로 (재검토)
