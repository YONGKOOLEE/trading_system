"""키움 모의 호출 제한(1700) 유도 실측 — ka10001 조회만 (04 PV-12, kiwoom-kr K-U10, 승인 A-097 ③). 사람이 직접 실행한다.

하는 일: ka10001 주식기본정보(종목 1개, 기본 005930)를 간격 1.0 → 0.5 → 0.25 → 0초 단계로(단계마다 최대 10회) 보내
1700(1701·1702)이 처음 나오는 간격을 찾고, 나오면 그 단계를 바로 멈춘 뒤 1·2·4·8·16초 대기로 정상 응답이 돌아오는
최소 대기를 기록하고 끝낸다. 전체 상한 80회·3분. 418 등 차단·인증 오류·알 수 없는 오류면 즉시 중단. 끝나면 토큰 폐기.
주문 API는 부르지 않는다(HTTP 경계 허용 목록: au10001·au10002·ka10001).
PAPER run·자동 운영이 꺼진 장외 시간에만 실행한다. 거래일 06:30~17:00 KST, 실행 잠금(run.lock·autopilot.lock),
하트비트 30초 이내, TRADE_MODE가 PAPER가 아님, 모의 도메인이 아님, connection_approved = true가 아님 → 연결 없이 거부.
결과: var/paper/probe/<YYYYMMDD-HHMMSS>-rate-probe.json(기계용)·.md(사람용). 가격·응답 원문·토큰·계좌번호는 남기지 않는다.

사용 (PowerShell, 저녁·주말·휴일에):
    cd C:\\AI\\trading_system\\trading_system
    $env:TRADE_MODE='PAPER'
    .venv\\Scripts\\python scripts\\rate-probe.py --mode PAPER

    선택: --code 005930, --intervals "1.0,0.5,0.25,0", --per-step 10, --max-calls 80, --max-seconds 180

기대 출력 (예시 — 실제 간격·대기 값은 서버가 정한다):
    호출 제한 실측 시작: ka10001 005930 / 간격 1.0 → 0.5 → 0.25 → 0.0초, 단계마다 최대 10회 / 상한 80회·180초
    토큰 발급 au10001: 정상
      간격 1.0초: 10회 {'0': 10} — 단계 완료
      간격 0.5초: 1회 {'1700': 1} — 1700 수신 — 단계 중단
    첫 1700: 2026-..T..+09:00, 단계 0.5초 1번째, 직전 간격 0.5초, [1700:...]
      회복 대기 1초: 코드 0
    결과: 1700 유도·회복 측정 완료 — 회복에 필요한 대기 1초
    토큰 폐기 au10002: 완료
    기록: ...\\var\\paper\\probe\\<시각>-rate-probe.json
    요약: ...\\var\\paper\\probe\\<시각>-rate-probe.md
종료 코드: 0 = 측정 완료(1700 유도·회복 또는 "1700 미발생"), 1 = 중단·회복 안 됨·상한 도달, 2 = 실행 전 거부(연결 없음).
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trading_system.app.rate_probe import rate_probe_main  # noqa: E402

if __name__ == "__main__":
    sys.exit(rate_probe_main(ROOT, sys.argv[1:]))
