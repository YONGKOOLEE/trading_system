"""검증 실행기 (T-026, CP-030 D-170). 사람이 직접 실행한다. 본체는 trading_system.app.validation_tool.

사용 예 (BACKTEST 전용, 네트워크·브로커 없음):
  .venv/Scripts/python scripts/validate-strategy.py --strategy trading_system.strategy.trend_sma:TrendSmaV1 \
      --params config/strategies/trend-sma-v1.toml --data <일봉 CSV 폴더> --report-version 1 --eval-time 09:30
최종 확인 구간(2026-01-01 이후)은 --final-check --confirmed-by A-xxx 로만, 전략 버전당 1회.
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from trading_system.app.validation_tool import validate_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(validate_main(sys.argv[1:]))
