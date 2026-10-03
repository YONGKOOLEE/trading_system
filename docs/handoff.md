# 세션 인계 (짧게 유지 — 60줄 이내, 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-03 16:05 / PAPER-0 전 전체 검토·수정 완료 / 다음: **사용자 확인 → (선택) main 병합 → PAPER-0 실행(사용자)**
- 브랜치(로컬만, push·병합 없음): … → feat/T-016-fix → **feat/T-017-prep (최신, A-044 결과)**
- 테스트 `.venv\Scripts\python -m pytest -q` → 902 passed (약 4분)

## 현재 상태
- 6단계 구현. 완료: T-000~T-015, T-020, T-021. T-016 준비 완료 + PAPER-0 전 검토 수정 완료(실행 대기).
- 승인본: 업무정의서 v0.6, 00 v0.4, 01 v0.6, 02 v0.7, 03 v0.7, 04 v0.6, 05 v0.5. 마지막 승인 A-043.
- 전략 paper0-verify v1 = **PAPER** (A-040·A-041). SNTP 승인 A-042. 실전 주문 코드·LIVE 승인 없음.
- 검토 기록: `docs/changes/review-2026-10-03-pre-paper0.md` (RV-01~25 + 2차 검증 F1~F9·재검토2·장애 e2e).
- 주요 변경: 감시 스레드 분리(KILL 즉시, D-038), K5 경로별 해제(link:rest/realtime/thread), 단일 실행 잠금,
  운영 로그 `var/paper/logs/ops-*.jsonl`·콘솔 경고, status 보강, 일봉 갱신, 장중만 주기 대조, 재조회 전 2초 대기,
  조회 연속 3회 실패 → K5, 일일 손익 = 기준 현금 + 내부 체결 현금흐름(DAILY_BASE 저장).

## 다음 세션 첫 행동
1. 사용자 확인 요청: **`docs/decision-sheet.md`** (차단 Q-1 예수금·Q-2 U-06·Q-3 병합, DP-28~37 권장안)
2. 사용자 지시 시 feat/T-016-fix → main 병합(D-085)
3. PAPER-0 실행 안내: `docs/strategies/paper0-verify/pv-procedure.md` v0.2 (cd → preflight → run → 09:25까지 K8 재개)
4. 1일차 확인: K-U20 계좌번호 형식, K-U21 빈 결과 목록 키, K-U14 909=cntr_no, K-U16 첫 체결 후 K4 여부, entr·d2_entra 세 시점
5. 사용자가 "n일차 끝"이라 하면 `var/paper/trading.db`·`var/paper/logs/` 분석 → T-017

## 세션 시작 시 읽을 것
1. CLAUDE.md, 이 파일, `git status`, `docs/.status`
2. `docs/decisions-pending.md` 대기: DP-28~37
3. `docs/changes/review-2026-10-03-pre-paper0.md`, pv-procedure, kiwoom-kr §7b

## 도구 (사람 실행)
- PAPER 기록 집계(T-017 준비, 읽기 전용) `scripts\paper-review.py --mode PAPER` → var/paper/review/review-*.md
- 시작 점검 `scripts\preflight.py`(엔진 실행 중이면 거부), 전략 `scripts\strategy.py`, 운영 `scripts\{status,kill,resume,reconcile-resolve,backup,restore-test}.py --mode PAPER`
- 실행 `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run` (사용자만, 창 하나)
- 권한: `.claude/settings.local.json` — PowerShell·Bash·Edit·Write 자동, push·reset·rebase·merge·clean·pip install·.env 차단.

## A-044 결과 (2026-10-03 17:15~)
- 1 결정표 `docs/decision-sheet.md` · 2 PAPER 집계 `scripts\paper-review.py` · 3 전략 후보 `docs/strategies/candidates-2026-10-03.md`(S-Q1~4) · 4 LIVE 사전 조사 `docs/broker-specs/live-prep-2026-10-03.md`
- 브랜치 feat/T-017-prep (feat/T-016-fix 위). 마지막 전체 시험 902 passed

## 남은 위험·후속
- 시뮬레이터 응답 유실 e2e(`test_kiwoom_sim_lost_order_response_resolved_by_query_and_ws`) 간헐 실패 이력(재조회 대기 추가 후 재발 관찰).
- K5 중에는 청산도 막힘(01 승인 정책) — REST 장애가 14:30에 걸리면 청산은 다음 판정으로.
- 미처리: WS 스레드 생존 감시, 이전 형식 K5 행, K10 직접 호출, 알림 롤백 유실, 인덱스, 백테스트 다중 판정 시각(C절).
- 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거, 텔레그램 연결.

## LIVE 전 필수 (미뤄 둔 항목)
- 달력 KRX 원문 대조 + 사용자 검증 데이터(A-037), 2026-11-19 수능일 시간, 백업 외부 저장소(A-039), 텔레그램,
  노출된 모의 키 재발급, 실전 API·비용 재확인, 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자)

## 작업 방식
- 작업 하나 = 세션 하나 권장. 테스트 통과 → 로컬 커밋 → 이 파일 갱신. worklog 항목 15줄 이내.
- 병렬 에이전트는 파일 소유를 나눠 같은 작업 트리에서, 커밋은 리드만.
