"""전 종목 목록: 키움 모의 ka10099 조회만 (A-108). 사람이 직접 실행한다.

PAPER run을 끈 뒤에만 실행한다(실행 잠금·하트비트 30초 이내면 거부). 허용 API는 au10001·au10002·ka10099뿐. 끝나면 토큰 폐기.
결과: <out>.csv와 <out>.csv.codes-stocks.txt (fetch-daily --codes용, 코스피·코스닥 비ETF·ETN)

사용 예: $env:TRADE_MODE='PAPER'; .venv/Scripts/python scripts/list-stocks.py --mode PAPER --out var/backtest/data/kr-all/stocks.csv
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.list_stocks import list_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(list_main(ROOT, sys.argv[1:]))
