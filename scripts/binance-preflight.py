"""M2 시작 점검: 바이낸스 Spot 테스트넷 접속, 조회만 (T-M2-14, 승인 A-096). 사람이 직접 실행한다.

본체는 trading_system.app.m2_preflight. 이 스크립트는 바이낸스 세션 생성 함수(broker/binance/preflight)를 넘기기만 한다
(app은 바이낸스 패키지를 import하지 않는다 — 의존 경계 D-119).

조건(하나라도 어긋나면 연결 없이 거부, 종료 코드 2): M2 체크아웃(C:\\AI\\trading_system\\m2-binance)에서 실행,
--market M2, TRADE_MODE=PAPER, .env.paper(TRADE_MODE=PAPER + BINANCE_TESTNET_API_KEY·BINANCE_TESTNET_SECRET_KEY),
config/m2/app.paper.toml [paper] connection_approved = true(사용자가 켬), 시작 일치 표(PAPER·M2 → binance-spot-testnet,
testnet.binance.vision, 별칭 BN-), DB 메타 M2(없으면 통과), 실행 중 엔진 없음.
주문 API(신규·취소·정정·테스트 주문·OCO·cancelReplace 등)는 전송 전에 막는다. 결과는 var/paper/preflight/ 에 남는다.

사용 (PowerShell):
    cd C:\\AI\\trading_system\\m2-binance
    $env:TRADE_MODE='PAPER'
    C:\\AI\\trading_system\\trading_system\\.venv\\Scripts\\python.exe scripts\\binance-preflight.py --market M2
종료 코드: 0 통과 / 1 점검 실패(결과 파일 있음) / 2 연결 전 거부
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.m2_preflight import m2_preflight_cli  # noqa: E402
from trading_system.broker.binance.preflight import open_testnet_session  # noqa: E402

if __name__ == "__main__":
    sys.exit(m2_preflight_cli(ROOT, sys.argv[1:], open_testnet_session))
