"""PAPER run 정상 종료 요청 (A-079, CP-029 D-159): 정지 요청 파일 var/paper/STOP을 쓴다. **PAPER 전용.**
사용자가 직접 실행하거나, 사용자가 대화로 명시적으로 지시하면 에이전트가 실행한다. 본체는 trading_system.app.autopilot.

사용 예: .venv/Scripts/python scripts/ops-stop.py --mode PAPER
M2 체크아웃(config/m2 있음)에서는 --market M2 (CP-037 D-227 — M2 감독은 코드 0 종료를 의도된 정지로 보고 재시작하지 않는다).
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.autopilot import ops_stop_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(ops_stop_main())
