"""키움 모의 서버 조회·구독 실측 — 읽기 전용 (CP-032 D-180, 승인 A-085 Q-2). 사람이 직접 실행한다.

하는 일: ka10080 분봉(1·5분, 수정주가)·ka10079 틱 조회, WebSocket 0B·0D 등록(REG)·해지(REMOVE) 응답 확인.
주문 API는 부르지 않는다(HTTP 경계 허용 목록: au10001·au10002·ka10080·ka10079, WS: LOGIN·REG·REMOVE·PING 응답).
PAPER run·자동 운영이 꺼진 장외 시간에만 실행한다. 거래일 06:30~17:00 KST, 실행 잠금(run.lock·autopilot.lock),
하트비트 30초 이내, TRADE_MODE가 PAPER가 아님, 모의 도메인이 아님, connection_approved = true가 아님 → 연결 없이 거부.
결과: var/paper/probe/<YYYYMMDD-HHMMSS>-probe.json(기계용)·.md(사람용). 응답 원문·가격·토큰·계좌번호는 남기지 않는다.

사용 (PowerShell, 저녁·주말·휴일에):
    cd C:\\AI\\trading_system\\trading_system
    $env:TRADE_MODE='PAPER'
    .venv\\Scripts\\python scripts\\kiwoom-probe.py --mode PAPER --codes "005930,000660,069500"

    --codes를 빼면 config/app.paper.toml 종목(지금 005930 하나)만 본다.
    선택: --max-pages 5 (ka10080 연속 조회 쪽 수, 1~20), --back-days 30,90,180,365,730 (base_dt를 며칠 전으로),
          --ws-seconds 10 (등록 뒤 수신 대기 초, 0~60)

기대 출력 (기본값, 3종목이면 조회 최대 62회 × 1.2초 ≈ 1.2분 + WS 약 10초):
    모의 실측 시작: 종목 005930, 000660, 069500 / ka10080 범위 1·5분 / 최대 62회 조회 (...)
    토큰 발급 au10001: 정상
    REST 조회:
      ka10080 005930 범위1 paging: 5쪽 ...행 2026...~2026... (다음 쪽 있음)
      ...
    WebSocket:
      WS 로그인 응답 return_code=0
      WS REG 0B 005930: return_code=0 ...
      WS REMOVE 0B 005930: return_code=0 ...
    토큰 폐기 au10002: 완료
    결과: ...\\var\\paper\\probe\\<시각>-probe.json
    요약: ...\\var\\paper\\probe\\<시각>-probe.md
종료 코드: 0 = 모두 정상, 1 = 일부 실패(결과 파일의 '실패' 절), 2 = 실행 전 거부(연결 없음).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.probe_tool import probe_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(probe_main(ROOT, sys.argv[1:]))
