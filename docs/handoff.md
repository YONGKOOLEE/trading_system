# 세션 인계 (짧게 유지 — 60줄 이내, 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-02 밤 / CP-017 승인(A-027), 2차 재검토 결함 수정(3658c51) / 다음: **CP-018 §3 Q1~Q5 답 → 반영 → 묶음 B**

## 현재 상태
- 0~5단계 승인 완료(A-005~A-019), 구현 시작 A-022. 6단계 진행 중.
- 완료: T-000~T-006, 검토 수정 CP-016(568e02b), 2차 재검토 수정(3658c51, CP-018 §2).
- 테스트 `.venv\Scripts\python -m pytest -q` → 429 passed (약 53초, V-95 무작위 시험 약 26초 포함).
- 브랜치(모두 로컬, push·main 병합 안 함): … → `fix/review-2026-10-02` → `feat/T-003-gate` → `feat/T-004-order-state`
  → `feat/T-005-halts` → `feat/T-006-fault-injection` → `fix/review-2026-10-02b`. 다음 작업은 맨 위 브랜치에서 분기.
- 미커밋 문서(커밋 정책상 사용자 지시 시): CP-017(approved), CP-018(draft), 02 v0.6·03 v0.4·04 v0.4·05 v0.3과 archive,
  handoff, worklog, approvals(A-026·A-027), .status.
- 현재 승인본: 업무정의서 v0.5, 00 v0.3, 01 v0.4, 02 v0.6, 03 v0.4, 04 v0.4, 05 v0.3 (A-027). **승인 대기: CP-018**.

## 세션 시작 시 읽을 것 (이것만)
1. CLAUDE.md (자동), 이 파일, `git status`, `docs/.status`
2. CP-018 §3(사용자 결정 Q1~Q5)·§4(변경 제안) — 답이 있으면 반영부터
3. 작업 행: `docs/05-plan.md` §2의 해당 T 행 + 아래 표의 절. 그 밖은 Grep으로 해당 줄만.

| 작업 | 읽을 절 |
|---|---|
| T-007 전략 틀 | 00 §2.2~§2.11, 04 §3.6, 코드 `gate/ports.py`(StrategyFacts), `domain/halts.py`(K9) |
| T-008 대조 | 02 ST-07·D-034·D-035, 03 §6.3, 04 §3.5, 코드 `halt/manager.py`(report_condition) |
| T-010 시장 데이터 | 03 D-040·D-055~D-057·§11, 04 V-11·V-74~76, 코드 `gate/ports.py`(MarketFacts), `domain/market.py` |
| T-009 백테스트 | 04 §2, D-071~D-077 |
| T-011 저장소 운영 | 03 D-059·D-060·D-089, 04 V-104·V-106·V-108 |

## 구현 구조 요약 (묶음 A 이후)
- 게이트 입력은 `gate/ports.py` 계약(시세·종목·달력·거래 상태·호가·일일 손익·현금·전략 상태·시계). 모르면 위험 확대 거부.
- 정지 허용표 `domain/halts.py`, 정지 관리 `halt/manager.py`(조건 이름은 REQUIRED_CONDITIONS), 운영 명령 `ops/commands.py`,
  로컬 도구 `scripts/kill·resume·status.py` → `ops/cli.py`.
- 결과 불명 해소 `execution/resolver.py`, 재시작 `Executor.recover_on_start()`.
- 시나리오 DSL `tests/support/scenario_dsl.py`, 게이트 가짜 입력 `tests/support/gate_fakes.py`.

## 남은 위험·후속 (작업별)
- T-007: StrategyFacts 실제 구현(전략 상태·V-113 PAPER 거부), K9 재개와 전략 상태 전환(INV-13), 손절가 신호 필드(CP-017 T3-6)
- T-008: 대조가 K3·K4·K5·K8 조건(`reconciled`·`recovery_ok`)을 보고, EVENT_ANOMALY·LATE_FILL → K4, 수동 소유권(INV-16), 잔고 조회 통합(C-10)
- T-009: 앱 배선(운영 명령 1초 주기 처리, 시작 시 recover_on_start + K8), var/<mode>/<실행ID>
- T-010: MarketFacts 실제 구현(달력·호가·거래 상태), 시세 신선도 설정 항목(CP-017 T3-11)
- T-012·T-013: ClockHealth·알림 전송(감사 ALERT 읽기)·원격 인증, K6·K11 조건 보고
- T-014: 키움 체결 조회의 체결번호 출처(CP-017 T4-9)

## 사용자에게 남은 결정·확인
- CP-018 §3 Q1 동시성 모델, Q2 PAPER-0 검증 전략 예외, Q3 손절가·L1, Q4 인증 오류 분류, Q5 시세 신선도 60초
- PAPER 전: IP·단말기 요건, ETF·ETN 모의 거래, 휴장일, 차트 API 비용·약관, 감사 보존 기간(C-6), 시세 신선도 값

## 작업 방식 (토큰 절약)
- 작업 하나 = 세션 하나 권장. 무인 묶음 진행 시 작업마다 테스트 통과 → 로컬 커밋 → 이 파일 갱신.
- 끝날 때: 이 파일 덮어쓰기 + worklog 항목은 10줄 이내.
