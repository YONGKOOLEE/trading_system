"""로컬 운영 도구: watchdog (03 D-054). 본 프로세스와 따로 사람이 실행한다. 알림만 하고 재시작하지 않는다.

사용 예: .venv/Scripts/python scripts/watchdog.py --mode PAPER
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.watchdog import watchdog_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(watchdog_main())
