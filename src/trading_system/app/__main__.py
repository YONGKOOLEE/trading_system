"""`python -m trading_system.app run|backtest` — 본체는 trading_system.app.main (T-020).

05-plan은 `python -m trading_system`으로 적었으나 03 §7 의존 규칙 표에 최상위 `__main__`이 없어(규칙 변경은 CP)
app 패키지 안에 둔다 (결정 대기 DP-23).
"""

import sys

from trading_system.app.main import main

if __name__ == "__main__":
    sys.exit(main())
