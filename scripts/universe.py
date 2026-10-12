"""U-B 사람 목록 제출 도구 (CP-034 D-198, A-092 Q-8). 사람이 직접 실행한다. 본체는 trading_system.app.universe_tool.

로컬 도구만(원격·텔레그램 경로 없음). BACKTEST·PAPER만, LIVE 거부.

사용 예:
  .venv/Scripts/python scripts/universe.py submit --mode PAPER --list kr-picks --file picks.csv
  .venv/Scripts/python scripts/universe.py show --mode PAPER --list kr-picks
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.universe_tool import universe_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(universe_main())
