"""PAPER 무인 자동 운영 (T-025, CP-029 draft, A-078). 작업 스케줄러가 평일 06:45에 실행한다(등록은 사용자가
scripts/schedule-register.ps1로). 사람이 직접 실행해도 같다. **PAPER 전용 — LIVE·BACKTEST는 거부.** 본체는
trading_system.app.autopilot.

사용 예: .venv/Scripts/python scripts/autopilot.py --mode PAPER --help
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.autopilot import autopilot_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(autopilot_main())
