"""분봉 축적: 키움 모의 ka10080 조회만 (T-028, CP-032 D-177, 승인 A-095 ②). 사람이 직접 실행한다.

PAPER·모의 도메인만. 거래일 06:30~17:00 KST, PAPER run·자동 운영 실행 중이면 거부(연결 없음). 허용 API는 au10001·au10002·ka10080.
정규장 봉만 `<out>/<코드>-<길이>m.csv`에 이어 쓰고(이미 있는 봉은 건너뜀), `<out>/meta/minute-fetch-<시각>.json`을 남긴다.
매일 저녁 실행해 보관 기간을 늘리는 용도다. 사용법·기대 출력: src/trading_system/app/minute_fetch.py 머리말.

사용 예: $env:TRADE_MODE='PAPER'; .venv/Scripts/python scripts/fetch-minute.py --mode PAPER --codes-file <파일> --minutes 1 --since 2025-10-01 --out var/backtest/data/kiwoom-ka10080-adj
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.minute_fetch import minute_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(minute_main(ROOT, sys.argv[1:]))
