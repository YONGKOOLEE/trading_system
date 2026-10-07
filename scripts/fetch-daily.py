"""백테스트용 일봉 수집: 키움 모의 ka10081 수정주가 조회만 (T-018, 승인 A-073 E·F). 사람이 직접 실행한다.

PAPER run을 끈 뒤에만 실행한다(실행 잠금·하트비트 30초 이내면 거부). 연결 조건은 시작 점검과 같다
(TRADE_MODE=PAPER, .env.paper, connection_approved = true). 주문 API는 전송 전에 막는다. 끝나면 토큰 폐기.
결과: var/backtest/data/kiwoom-ka10081-adj-<마지막 확정일>/<코드>.csv, meta/universe.csv·excluded.csv·fetch-summary-<시각>.json

사용 예: $env:TRADE_MODE='PAPER'; .venv/Scripts/python scripts/fetch-daily.py --mode PAPER --top 30
안내: docs/strategies/trend-sma/data-fetch.md
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.daily_fetch import fetch_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(fetch_main(ROOT, sys.argv[1:]))
