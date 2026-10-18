"""LF-8 표본 목록 생성 (CP-038 D-244, T-073). 조회 전용 — A-118 일봉 사본만 읽는다. 네트워크·브로커·주문 없음.

사용 예: .venv/Scripts/python scripts/lf8-list.py --data-dir var/backtest/data/binance-public-usdt-1d-20261018 --out docs/strategies/lf8/lf8-list.csv
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.lf8_list import lf8_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(lf8_main(ROOT, sys.argv[1:]))
