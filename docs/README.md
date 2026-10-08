# docs/ — 계획·승인·기록

기준본(approved)과 개정 draft가 함께 있다. **게이트 판정은 approved 버전 기준**이며 draft는 `[CP-xxx]` 표시로 변경을 누적한다(승인은 문서 묶음으로, 검토 요약 `changes/draft-bundle-review-2026-10-07.md`). 상태 기준일 2026-10-08.

| 파일/폴더 | 역할 | 단계 | 게이트 | 현재 |
|---|---|---|---|---|
| business-definition.md | 업무정의서 (범위·R-ID 원천) | 사전 | — | v0.6 approved (A-029) / v0.7 draft (CP-024) |
| 00-strategy.md | 전략 정의 형식·등록 절차 | 0 | strategy:approved | v0.4 approved (A-029) / v0.5 draft (CP-024·CP-030) |
| capabilities.md | 시장별 능력(CAP) 지원 목록 — **실행 코드가 읽음, PAPER 중 직접 수정 금지** | 0~ | — | 2026-10-05판. M1 일부 "검증 중". 변경 선반영은 `changes/CP-024-capabilities-draft.md`(CAP-ORD-02 → 미지원, A-084) |
| templates/strategy-template.md | 개별 전략 문서 양식 | 0~ | — | A-015 (CP-014) + [CP-030] 공통 성과 기준 칸(draft) |
| strategies/<전략ID>/ | 개별 전략 명세·백테스트·상태 기록 | 0~ | 전략별 | paper0-verify(v1 STOPPED·v2 PAPER, pv-procedure v0.6), trend-sma(초안, 30종목 백테스트), m2-paper0-verify(초안, 미승인) |
| 01-requirements.md | 요구사항·리스크 정책 | 1 | requirements:approved | v0.8 approved (A-060) / v0.9 draft (CP-024~CP-031) |
| 02-data-model.md | 범용 논리 데이터 모델 | 2 | data-model:approved | v0.7 approved (A-029) / v0.8 draft (CP-024·CP-027) |
| 03-design.md | 시스템 구조·기술 선택 | 3 | design:approved | v0.9 approved (A-060) / v1.0 draft (CP-024~CP-031) |
| broker-specs/ | 브로커 API 확인 기록 (출처·확인일·버전) | 3~ | — | kiwoom-kr.md(2026-10-01 + ② 모의 실측 §7), krx-market.md(2026-10-01), binance-spot.md v0.3(M2), live-prep-2026-10-03.md, README.md |
| 04-validation.md | 검증 계획 | 4 | validation:approved | v0.8 approved (A-060) / v0.9 draft (CP-024~CP-031, V-01~V-234) |
| 05-plan.md | 구현 계획 (T-ID) | 5 | plan:approved | v0.5 approved (A-033) / v0.6 draft (CP-024·026·027·029·030, T-000~T-026·T-M2) |
| 07-paper-review.md | PAPER 검토·LIVE 인계 (시장별) | 7 | — | v0.3 draft — M1 PAPER-0 1~3일차 기록(3일차는 보고 기반), 종료 판정 아님 |
| approvals.md | 사용자 승인 기록 (사용자 승인만 기록) | 전체 | — | A-001~A-084 |
| .status | 현재 단계 보조 기록 (승인 근거 아님) | 전체 | — | 2026-10-08 |
| worklog.md | 세션별 작업 기록 | 전체 | — | (1)~(39) |
| handoff.md | 세션 인계(현재 상태·다음 행동·도구·위험) — 작업 끝마다 덮어씀 | 전체 | — | 2026-10-08 저녁 |
| decisions-pending.md | 무인 진행 중 에이전트 임시 결정 대기열(DP-xx, A-030) | 6~ | — | DP-1~DP-43 (재검토 대기: DP-31·DP-40) |
| decision-sheet.md | 사용자 확인 결정표(PAPER-0 전, A-044) | 6 | — | 처리 완료(2026-10-04), 기록용 |
| user-manual.md | 사용 설명서(평소·이상 시·원격·전략 변경·끄고 켜기) | 6~ | — | v0.1 초안 (main 병합 2026-10-09). 3일차 결과 반영 v0.2 예정 |
| process/ | 에이전트 단계별 가이드 (계획 / 구현) | — | — | planning-guide, implementation-guide |
| templates/ | 산출물 양식 | — | — | stage-doc-template, strategy-template |
| changes/ | 승인 문서 변경 제안 CP-xxx, 검토 기록 | — | — | CP-016~CP-031 (목록·상태는 changes/README.md) |
| archive/ | superseded 또는 기준본 보존 | — | — | 업무정의서 v0.3~v0.6, 00 v0.3·v0.4, 01 v0.3~v0.8, 02 v0.2~v0.7, 03 v0.2~v0.9, 04 v0.2~v0.8, 05 v0.2~v0.5, 전략 양식(A-005) |

- 6단계(구현)는 별도 문서가 없다. 작업 기록은 worklog.md에, 작업 정의는 05-plan.md에 둔다.
- 다중 시장 문서는 공통 본문과 시장별 부록(M1/M2/M3)으로 나눈다.
- 세션 시작 순서: `.status` → `approvals.md` 끝 → `handoff.md` → `worklog.md` 마지막 항목 → 현재 단계 가이드(process/).
