# M2 PAPER-0 시작 — 사용자 가이드 (복사해 붙이는 명령과 기대 결과)

- 작성: 2026-10-18 (세션 50). 원본 절차: [strategy.md](strategy.md) "PV-M2 실행 절차". 이 문서는 그 절차를 사용자가 할 순서로 다시 묶은 것이다.
- 대상: 바이낸스 **테스트넷**(가상 자산)에서 시험 전략 m2-verify를 돌려 주문·체결·대조를 확인한다. 실제 돈·실계정과 무관하다.
- 전략: m2-verify v1 — BTCUSDT, 매일 KST 10:00 매수(10 USDT), KST 13:00 매도. 상태 전환 승인 **A-112**.
- 모든 명령은 PowerShell. 파이썬은 M1 폴더의 가상환경을 쓴다(전체 경로 그대로 복사).
- 비밀값(토큰·키)은 대화·텔레그램에 붙이지 않는다.

## 순서 한눈에
| # | 할 일 | 언제 | 걸리는 시간 |
|---|---|---|---|
| 1 | M2 텔레그램 봇 만들기 | 아무 때나 (휴대폰 + PC) | 10분 |
| 2 | 대화에서 **테스트넷 주문 전송 승인** | 아무 때나 | 1분 |
| 3 | 에이전트가 시작 전 차단 항목 정리 (V-157 잔여 4개, T-050 기록) | 에이전트 | — |
| 4 | 실행 날: 관찰 run 정지 → M2 폴더 갱신 → 설정 → 등록 → 기준선 → 시작 → 지급 BTC 매도 | **집에 있는 날 아침, KST 10:00 전** (주말 권장) | 30~60분 |
| 5 | 매일 확인 (텔레그램) | KST 10:00·13:00 무렵, 저녁 | 각 1분 |

1·2는 지금 해도 된다. 4는 3이 끝났다고 에이전트가 알려 준 뒤에 한다.

## 1. M2 텔레그램 봇 만들기
M1 봇과 **다른 봇**이다(시장별 봇, D-117 — 승인 A-109).
1. 텔레그램에서 `@BotFather` → `/newbot` → 이름(예: `TS M2`) → 아이디(예: `ts_m2_<아무거나>_bot`). 받은 **토큰**을 복사해 둔다.
2. 새 봇 대화창에 아무 메시지나 1번 보낸다.
3. 채팅 ID 확인: PC 브라우저 주소창에 `https://api.telegram.org/bot<토큰>/getUpdates` (토큰 자리에 1에서 받은 값) → 화면의 `"chat":{"id":숫자` 숫자. (M1 봇의 채팅 ID와 같을 수 있다 — 본인 계정 번호.)
4. M2 비밀 설정 파일 열기:
```
notepad C:\AI\trading_system\m2-binance\.env.paper
```
5. 파일 끝에 두 줄 추가 후 저장(값은 본인 것으로, 따옴표 없이):
```
TELEGRAM_BOT_TOKEN=<1의 토큰>
TELEGRAM_CHAT_ID=<3의 숫자>
```
- 기대: 저장만 하면 끝. 실제 연결 확인은 4단계 run 시작 때.

## 2. 대화에서 승인할 내용 (에이전트에게 보내기)
아래를 그대로 보내면 된다:
> T-M2-15 테스트넷 주문 전송 승인 — m2-verify v1(BTCUSDT, 매일 KST 10:00 매수 10 USDT·13:00 매도), 지급 BTC `/exit` 매도(리셋 뒤 반복 포함)

- 에이전트가 승인 번호(예상 **A-125**)를 기록·커밋하고 알려 준다. 이 번호를 4-3에서 쓴다.
- 이 승인은 **테스트넷 주문만**이다. 실전 주문 코드·LIVE와 무관하다.

## 3. 에이전트 작업 (기다리면 됨)
- V-157 잔여 4개(V-02·V-76 부분, V-112·V-127) — M2 PAPER-0 시작 조건 "L1~L3 전부 통과"의 남은 항목. 시험 추가 또는 "해당 없음" 근거 기록.
- 4-1에서 관찰 run을 멈추면 T-050 관찰 결과(PV-M2-12)를 기록한다.
- 끝나면 "PAPER-0 시작 가능"이라고 알린다.

## 4. 실행 날 (집, KST 10:00 전에 4-7까지)
### 4-1. 관찰 run 정지
관찰 run이 돌고 있는 PowerShell 창(제목에 run-m2)에서 **Ctrl+C**. 창의 X로 닫지 않는다.
- 기대: 종료 문구가 나오고 프롬프트(`PS C:\...>`)로 돌아온다.
- 에이전트에게 "관찰 run 멈춤"이라고 보낸다(T-050 기록용, 에이전트가 읽기만 한다).

### 4-2. M2 폴더를 최신 코드로 갱신
```
git -C C:\AI\trading_system\m2-binance status --short
```
- 기대: ` M config/m2/app.paper.toml` 한 줄(지난번 켠 `connection_approved = true`). 다른 줄이 있으면 멈추고 출력을 보낸다.
```
git -C C:\AI\trading_system\m2-binance checkout -b m2/run3 main
```
- 기대: `M	config/m2/app.paper.toml` 과 `Switched to a new branch 'm2/run3'`. 오류("would be overwritten" 등)면 멈추고 출력을 보낸다.
- **2의 승인 기록 커밋 뒤에** 해야 한다(시작 검사가 M2 폴더의 승인 기록을 확인함, DP-66). 에이전트가 "커밋함"이라고 한 뒤에 할 것.

### 4-3. 설정 파일 편집
```
notepad C:\AI\trading_system\m2-binance\config\m2\app.paper.toml
```
1. `[paper]` 아래를 이렇게(첫 줄은 이미 true):
```
connection_approved = true
orders_approved = true
orders_approval = "A-125"
```
   (`A-125` 자리에 2에서 받은 번호)
2. `[telegram]` 아래 `enabled = false` → `enabled = true`
3. 파일 맨 끝에 추가:
```
[[strategies]]
module = "trading_system.strategy.m2_verify:M2Verify"
params = "config/strategies/m2-verify-v1.toml"
eval_times = ["01:00", "04:00"]
verification_approval = "A-112"
```
4. 저장.

### 4-4. 전략 등록·상태 전환 (run 꺼진 상태)
```
Set-Location C:\AI\trading_system\m2-binance
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py register --mode PAPER --market M2
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py transition --mode PAPER --market M2 m2-verify 1 BACKTEST_PASSED --approval A-112 --evidence docs/strategies/m2-verify/backtest-v1.md
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py transition --mode PAPER --market M2 m2-verify 1 PAPER --approval A-112
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\strategy.py show --mode PAPER --market M2
```
- 기대: 첫 줄 `등록 통과: m2-verify:1 (PAPER 대상, PAPER-0 검증 승인 A-…)`, 마지막 `show`에 `m2-verify v1 M2 PAPER`.
- `거부`·`전이 거부`가 나오면 멈추고 출력을 보낸다(아무것도 바뀌지 않는다).

### 4-5. 기준선 확정 (처음 한 번)
```
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\reconcile-resolve.py initial-baseline --mode PAPER --market M2
```
- 기대: `initial-baseline 완료: USDT 기준선 …, 유니버스 보유 {'BTCUSDT': '1'} (수동 보유로 기록)`.
- "이미 기준선이 있음 … reset-confirm" 문구면 관찰 run 때 이미 한 것 → 정상, 다음 단계로.

### 4-6. run 시작 (이 창은 계속 열어 둔다)
```
Set-Location C:\AI\trading_system\m2-binance
$env:TRADE_MODE='PAPER'
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\run-m2.py
```
- 기대: `주문 전송 스위치: 주문 전송 허용 (승인 A-125 …)`, `바이낸스 서버 시각 오차 …ms`, `시작: 시장 M2, 모드 PAPER, 계좌 BN-PAPER-01 …`. 텔레그램 M2 봇에 시작 알림(`[M2]`).
- 시작 직후는 항상 K8(주문 없음) — 정상.
- 시작 거부 문구가 나오면 그 줄을 보낸다.

### 4-7. K4·K8 재개 → 지급 BTC 1개 매도 (KST 10:00 전)
기준선 확정(4-5)은 열린 대조 항목만 닫고 **K4 정지는 풀지 않는다**(관찰 run에서 걸린 K4 — [t050-observation.md](t050-observation.md)). run 시작 뒤 1~2분 기다려 대조가 일치한 다음 순서대로 푼다.
텔레그램 **M2 봇**에서:
1. `/status` → "열린 대조 불일치 0건"(또는 대조 MATCH)과 정지 K4·K8 확인. 불일치가 남아 있으면 멈추고 출력을 보낸다.
2. `/resume K4` → 회신에 나온 `/confirm <코드>`를 60초 안에 보냄. 기대: "K4 재개 완료".
3. `/resume K8` → `/confirm <코드>`. 기대: "K8 재개 완료".
4. `/status` → 정지 없음, BTCUSDT 1 수동 보유 확인.
5. `/exit BTCUSDT 1` → `/confirm <코드>`. 기대: 시장가 매도 체결 알림, 이후 보유 0.
- 텔레그램이 안 되면 PC의 다른 PowerShell 창에서(같은 순서, K4 먼저):
```
Set-Location C:\AI\trading_system\m2-binance
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\resume.py --mode PAPER --market M2 K4
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\resume.py --mode PAPER --market M2 K8
```
- 부분 체결로 남으면 남은 수량으로 `/exit`를 다시(0.00001 단위가 아니면 거부 — 자동 반올림 없음).
- 10:00 전에 못 하면 그날 전략은 아무것도 안 한다(안전). 다음 날 10:00 전에 하면 된다.

## 5. 매일 확인
| 시각(KST) | 어디서 | 정상 |
|---|---|---|
| 10:00 무렵 | 텔레그램 M2 | 매수 체결 알림(체결 0이면 EXPIRED — 그날 매수 없음, 이상 아님) |
| 13:00 무렵 | 텔레그램 M2 | 매도 체결 알림 |
| 아무 때 | 텔레그램 M2 | `/status`, `/review` |
| 저녁(PC) | 아래 명령 | 활성 정지 없음, 결과 불명 0, 대조 MATCH, BTCUSDT 잔량 0.00001 미만 |
```
Set-Location C:\AI\trading_system\m2-binance
C:\AI\trading_system\trading_system\.venv\Scripts\python.exe scripts\status.py --mode PAPER --market M2
```
- PC는 켜 둔다(10:00·13:00에 프로세스가 돌아야 함). M2는 무인 자동 재시작이 아직 없다 — 재부팅되면 4-6·4-7을 다시.

## 정지
- 보유 없는 시간(KST 13:10 이후)에 run 창에서 **Ctrl+C**, 또는 다른 창에서:
```
New-Item -ItemType File C:\AI\trading_system\m2-binance\var\paper\STOP
```
- 긴급: 텔레그램 `/kill 사유` — 신규 매수만 막는다(**자동 청산 아님**).

## 이상할 때
- 출력이 기대와 다르면 그 단계에서 멈추고, 명령과 출력 전체를 에이전트에게 보낸다. 같은 명령을 여러 번 반복하지 않는다.
- 테스트넷은 약 한 달에 한 번 리셋된다(잔고 재지급 → 대조 불일치 K4). 그때는 에이전트에게 알리면 `reset-confirm` 절차를 안내한다.
