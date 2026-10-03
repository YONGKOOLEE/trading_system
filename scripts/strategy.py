"""전략 등록·상태 운영 도구 (R-17, INV-13). 사람이 직접 실행한다. 본체는 trading_system.app.strategy_tool.

사용 예:
  .venv/Scripts/python scripts/strategy.py show --mode PAPER
  .venv/Scripts/python scripts/strategy.py register --mode PAPER
  .venv/Scripts/python scripts/strategy.py transition --mode PAPER paper0-verify 1 BACKTEST_PASSED --approval A-xxx --evidence <보고서>
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.strategy_tool import strategy_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(strategy_main())
