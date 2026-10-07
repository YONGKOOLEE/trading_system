# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-08 새벽 / **3일차(10-08)는 "보는 자동 운영"**(A-080 중간안): 사용자가 06:45에 autopilot 1회 직접 실행·화면 확인. main = CP-028(운영 복원력)·CP-029(자동 운영, 대화 지시 실행 A-079) 병합 완료. 마지막 기록 **A-080**. 운영 카드: https://claude.ai/artifact/XXZrAhidXPAFo1hUDRgVdw
- **체크아웃 `main`** (HEAD f639b9c 이후: T-025 자동 운영 c17651e, trend-sma 30종목 백테스트 보고서 f639b9c). 전체 시험: autopilot 브랜치 1252 passed, 병합 main 결과는 worklog (38). push 없음.
- 별도 브랜치(미병합): M2 `feat/T-M2-01-min-e2e`(1919a8a), `feat/T-M2-10-adapter`(2a9f9ba), `feat/T-M2-02-03`(3a2164d). worktree: m2-binance, m2-adapter, m2-next, cp028, autopilot, t018-multi(모두 병합 끝난 것은 cp028·autopilot·t018-multi — 정리는 사용자 확인 뒤).
- 어젯밤 대화 지시(A-079) 첫 사용: `resume.py --mode PAPER K1` 기록(local-b53ec44e…) — 3일차 run 시작 때 처리돼 K1 해제 → 자동 운영이 K8 자동 재개.
- 실전 주문 코드·LIVE 승인 없음. 게이트 변경 없음(01~05·업무정의서·00은 draft, 검토 요약 docs/changes/draft-bundle-review-2026-10-07.md).

## 다음 세션 첫 행동
1. `git status`(main)·이 파일·`docs/approvals.md` 끝 3행·`docs/worklog.md` 마지막 항목 확인.
2. 사용자가 아침 자동 운영 결과(텔레그램·창 출력)를 주면 확인: "시작 점검 통과, run 시작" → K9(REPLACE) 경고 → K1 해제 → "K8 자동 재개 완료". 문제면 `var\paper\logs\autopilot-20261008.log`·`run-console-20261008.log`·`status`로 원인 파악. 출발 시간이 급하면 수동 절차(카드 PC 명령)로 돌리게 안내.
3. 장중: 원격 접속으로 `/review` 결과를 받으면 확인(09:35 보유 1, 14:35 보유 0이 정상 — v2).
4. 16시 전후 일일 요약 확인 뒤 3일차 분석: day2-analysis.md 조회(날짜 2026-10-08)·paper-review(슬리피지·조회 주기 첫 측정, 수수료 0.35% 추정 vs 키움 앱)·07-paper-review 3일차·autopilot 첫 모의 서버 실행 결과(V-212). 확인할 것: STRATEGY_SYNC 3건·K9, K1→K8 자동 재개 시각, v2 09:30 매수·14:30 매도, quote_snapshot 행, VI 영문 코드 경고 0건, 07:53 K5와 CP-028 P4 장 전 재대조로 09:00 전 해제되는지, 15:45 종료 절차·백업·요약.
5. 성공이면 사용자에게 예약 등록 안내(관리자 PowerShell, pv-procedure §3.5) → 10-12(월)부터 무인. 10-09는 한글날 휴장.
6. 사용자는 터미널 명령에 익숙하지 않다 — 실행 안내는 **복사해 붙일 전체 명령**, 용어는 풀어서. "실전"은 LIVE에만 쓴다(모의 서버 실행은 "모의 서버 실행").

## 3일차(2026-10-08) 아침 사용자 절차 — 안내 완료
1. PC 켜고 로그인 → VS Code 열기.
2. 06:43 시간 동기화(설정 → 시간 및 언어 → 날짜 및 시간 → "지금 동기화") — 예약 작업 미등록이라 손으로.
3. 06:45 PowerShell 창 하나(15:45 이후 끝날 때까지 닫지 않음):
   ```
   cd C:\AI\trading_system\trading_system
   $env:TRADE_MODE='PAPER'
   .venv\Scripts\python scripts\autopilot.py --mode PAPER
   ```
4. 텔레그램 "시작 점검 통과, run 시작" → "정지 K9 발동(REPLACE)"(정상) → "K8 자동 재개 완료"(K1 먼저 해제, 5분 안팎) 확인 뒤 출발.
5. 출발 전 VS Code 대화창 `/remote-control` → 회사에서 claude.ai/code 또는 Claude 앱 Code 탭.
6. 09:35·14:35 텔레그램 `/review`. 16시 전후 "일일 요약"이 오면 끝(paper-review·백업·종료 자동). 퇴근 후 키움 앱 체결·잔고만 확인해 알려 준다.
- 이상: 06:50까지 "run 시작" 없음 → 창 메시지를 원격으로 보냄. 급하면 Ctrl+C로 끄고 수동 절차.

## 결정 필요 (급하지 않음)
1. 전략은 뒤로(사용자 2026-10-08: 지금은 시스템 검증·사용법 우선). 보존: trend-sma 초안·30종목 백테스트(docs/strategies/trend-sma/backtest-v1-multi.md), 스크리닝 아이디어(전 종목 대상, 거래 가능 필터 → 추세 판정 → 상대강도 순위, 보유 20) — 전략 착수 때 Q1~Q5로.
2. 시스템 과제 목록에 추가: 동적 유니버스, 다종목 동시 운영(보유 20), 정정 주문(CAP-ORD-03), 지정가·호가 단위(X-U1), 게이트 L1(시장가 상한가 기준)과 전략 위험 계산 정합.
3. M2 [제안] 12건(T-M2-02·03 6건, 어댑터 6건), CP-027 호가 단위 문서·코드 차이, V-180 보유 중 교체 처리.
4. 문서 승인 묶음(CP-025 먼저 — 코드는 main에 있음).
5. 사용 설명서(평소·이상 시·원격·전략 변경·끄고 켜기) — 자동 운영 첫 실행 결과 반영 뒤 작성.

## 진행 중 결정·작업
### 원격 운영 (사용자 요청 2026-10-06 — 회사에서 장중 조치 불가)
- **방법 1 — CP-026 승인(A-070), 문서 draft 반영 완료, 병합 대기**. worktree `C:\AI\trading_system\remote-ops`, 브랜치 `feat/T-022-remote-ops`(코드 0b6e5a9, 문서 f098414). CP: `docs/changes/CP-026-remote-ops.md`(그 브랜치에만 있음). **병합 지시 전 PAPER 실행 브랜치에 넣지 않음.** (원래 구상:) 매매 시스템 텔레그램 봇 확장. `/resume K8`(복구 조건 충족 시만), `/resume K4`(열린 대조 불일치 0건 + 최근 대조 MATCH 시만) 원격 허용, `/reconcile`(즉시 대조·결과 회신), `/review`(오늘 체결·정지·불일치 요약). K3·K5·K7은 로컬 유지. 확인 코드(`/confirm`) 유지. 대상 규칙: 01 §2.9(원격 재개 범위), 03 §6.6, 04 V-32. **별도 브랜치에서 구현·시험 → CP-026 → 승인 뒤 병합**. 2일차 실행본에는 넣지 않는다.
- **방법 2 (설치 불필요)**: Claude Code Remote Control(https://code.claude.com/docs/en/remote-control.md). 도구 기능이라 정책 변경 없음. 원격 지시를 받아도 승인 구조 그대로 — `run`·정지 재개는 사용자(D-086), 에이전트는 분석·개발·조회. 텔레그램 채널 플러그인(https://code.claude.com/docs/en/channels.md)은 시험판·Bun 설치 필요라 보류.

### 검증 전략 v2 전환 (보유 0건인 날 저녁)
- Ctrl+C → 에이전트가 `config/app.paper.toml` 전략 3줄 변경(module `trading_system.strategy.paper0_verify:Paper0VerifyV2`, params `config/strategies/paper0-verify-v2.toml`, verification_approval `A-067`) → 사용자:
  ```
  .venv\Scripts\python scripts\strategy.py register --mode PAPER
  .venv\Scripts\python scripts\strategy.py transition --mode PAPER paper0-verify 2 BACKTEST_PASSED --approval A-067 --evidence docs/strategies/paper0-verify/backtest-v2.md
  .venv\Scripts\python scripts\strategy.py transition --mode PAPER paper0-verify 2 PAPER --approval A-067
  .venv\Scripts\python scripts\strategy.py transition --mode PAPER paper0-verify 1 STOPPED --stop-reason REPLACE --approval A-067
  ```
- 사본 저장소로 사전 시험 성공(2026-10-06). 전환 뒤 `status`에 K9(paper0-verify:1) 1건 남는 것은 정상. v1 보유는 v2로 넘어가지 않으므로(D-011) 보유가 있으면 전환하지 않는다. `docs/strategies/paper0-verify/status.md`에 전환 행 추가.

### 승인 대기 묶음 (사용자)
- CP-025(D-121~D-135, V-162~V-180, 결정 Q-1~Q-9) — T-017 코드가 이미 구현됨. 01 v0.9·02 v0.8·03 v1.0·04 v0.9 draft에 반영.
- CP-024 반영 개정 draft 7종(업무정의서 v0.7, 00 v0.5, 01 v0.9, 02 v0.8, 03 v1.0, 04 v0.9, 05 v0.6) + `docs/changes/CP-024-capabilities-draft.md`(실제 capabilities.md는 M1 실행 코드가 읽으므로 승인·회귀 시험 뒤 반영).
- 개정 draft 미결: V-157 재실행 목록 V-41·V-48·V-125·V-126 기대값, 03 §16 미결 3건(dust 판정 시점, PENDING_NEW·PENDING_CANCEL, CANCEL_ONLY 알림), CP-024 본문 정오(§7 min→max 등), "M2 브랜치" 정의, CAP-ORD-03a/03b, 02 KST 날짜 혼용 정리의 M1 영향.
- 사용자 확인: 1일차 매수 수수료(실측 추정 약 970원 = 0.35%, 설정 0.015% — 키움 앱 체결 내역), NXT VI(`_NX`)를 우리 종목 VI로 볼지(현재 보수적), 07-paper-review 판단 필요 1~5.

## 현재 상태 요약
- **M1**: 6단계, PAPER-0 진행(1일차 10-06 완료). 전략 paper0-verify v1 PAPER, 005930 1주 보유(v1 소유) 중 — 2일차 09:30 매도 예정. 1일차 결함·수정: worklog (30)(31), kiwoom-kr §7c, 07-paper-review v0.1.
- **M2(바이낸스)**: 설계 CP-024 v0.5 승인(A-066). 구현은 M1 T-017 뒤 T-M2-01부터(A-065 Q1) + M2 "구현 시작" 지시 필요. 근거: CP-024, binance-spot.md v0.3, 검증 전략 초안 `docs/strategies/m2-paper0-verify/strategy.md`(미승인).

## 로드맵 (승인 단위)
| 순서 | 작업 | 선행·승인 |
|---|---|---|
| 1 | PAPER-0 2일차 이후(3~5거래일 권장) | 진행 중 |
| 2 | 원격 운영 방법 1 (T-022, CP-026) | 구현 완료(0b6e5a9). CP-026 승인 → 병합 |
| 3 | T-017 마무리: main 병합, v2 전환, CP-025·개정 draft 승인, DP-31·40 재결정 | 2일차 확인 |
| 4 | T-018 실제 전략 결정 → T-019 PAPER-1 | 사용자 결정 |
| 5 | M2 구현(T-M2-01부터) | T-017 완료 + M2 구현 시작 |
| 6 | LIVE 준비(시장별) | 별도 승인 |

## 도구 (사람 실행, 먼저 `cd C:\AI\trading_system\trading_system`)
- 점검 `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` (run 실행 중에는 거부)
- 실행 `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run` (사용자만)
- `.venv\Scripts\python scripts\<도구>.py --mode PAPER`: `status`, `resume K8|K4|K1`, `kill`, `reconcile-resolve`, `paper-review`, `telegram-test`, `backup`, `restore-test`, `strategy show|register|transition`
- 권한 `.claude/settings.local.json`: push·reset·rebase·clean·pip install·비밀 파일 접근·`rm -rf` 차단. **명령문에 비밀 파일 이름이 들어가면 거부**.

## 남은 위험·주의
- 텔레그램: 내용 거부 긴급 알림은 로컬에만 남음, 429 상한 이내 지연은 K11 없음(CP-025 R-4~R-8).
- K-U14 행 단위 미확인(부분 체결 여러 번인 날), 대조 합산이 많게 셀 수 있음 → K4(보수적).
- 낮음: 알림 롤백 유실, CLOSE 대조 지연, 같은 tick 확정+FOUND 이중 판정, 텔레그램 스레드 생존 감시 밖, status 정지 시각 UTC 표시.
- 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거.
- **문서 편집 주의**: heredoc·Python 문자열에 `scripts\resume.py` 같은 경로를 쓰면 `\r`이 줄바꿈으로 깨진다(10-06에도 발생). 경로가 든 문서 수정은 Edit 도구 또는 `chr(92)`로, 수정 뒤 `grep -n "^esume\|scripts$"`와 제어 문자 검사.
- 정리 필요(사용자): `C:\Users\user\AppData\Local\Temp\v91-p8kqynon` 임시 폴더.

## LIVE 전 필수
- 달력 KRX 원문 대조 + 사용자 검증 데이터(A-037), 2026-11-19 수능일 KRX 공지
- 실전 API 호출 제한·수수료·호가 단위(X-U1) 공식 원문
- 자동매매 전용 실계좌(A-051), 백업 외부 저장소(A-039), 노출된 모의 키 재발급, 밤샘 실행 검증(PV-11)
- 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자)

## 작업 방식
- 테스트 통과 → 로컬 커밋 → 이 파일 갱신, worklog 항목 15줄 이내.
- 수정 뒤 독립 재검토 1회 이상(10-06에도 높음 등급 4건 발견). 재검토 지적은 코드로 사실 확인 후 반영(V-3처럼 틀린 지적 있음).
- 병렬 에이전트는 읽기 전용 또는 파일 소유 분리, 커밋은 리드만. `docs/capabilities.md`는 실행 코드가 읽으므로 PAPER 중 직접 수정 금지.
