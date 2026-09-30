# 작업 기록

세션마다 한 항목씩 추가한다: 날짜, 수행 작업, 변경 파일, 실행 명령과 실제 결과, 미해결 질문·차단 조건, 다음 세션 첫 행동.

## 2026-10-01 — 지침·업무정의서 개선, 폴더 구조 생성
- 수행: CLAUDE.md를 공통 규칙과 단계별 가이드(계획 0~5 / 구현 6~7)로 분리. 업무정의서 v0.4 draft 작성(v0.3 기준본은 archive에 보존). docs 체계, .gitignore, .env.example, 폴더 골격 생성.
- 변경 파일: CLAUDE.md, README.md, .gitignore, .env.example, docs/**, config/README.md, src/trading_system/README.md, tests/.gitkeep, scripts/.gitkeep
- 실행 명령: `git status`(변경 없음 확인), `python --version` → 3.14.7, `py -0` → 3.14 64-bit 1개. 애플리케이션 코드·설치·브로커 연결 없음.
- 커밋: 하지 않음(커밋 정책 미승인).
- 미해결: 업무정의서 v0.4 CP-01~CP-10 승인 여부, U-18(단계 표현 해석), approvals.md에 v0.3 반영, 상위 폴더(C:\AI\trading_system)의 빈 git 저장소 처리, 커밋 정책.
- 다음 세션 첫 행동: v0.4 승인 결과 확인 → 0단계 00-strategy.md(전략 정의 형식) 초안 착수.

## 2026-10-01 (2) — v0.4 승인 반영, 커밋
- 사용자 답변: U-18 해석 맞음, CP-01~CP-10 전부 채택, R-21~R-27 채택, origin에 커밋 지시, 상위 폴더 git 확인·불필요 시 삭제.
- 수행: 업무정의서 v0.4 → approved, v0.3 → superseded 표시. approvals.md에 A-001~A-004 전사. .status·CLAUDE.md 커밋 정책 갱신.
- 상위 폴더 확인: `C:\AI\trading_system\.git`은 origin(같은 URL)만 설정되어 있고 커밋·객체·ref 없음 → 불필요로 판단. 삭제 시도는 도구 권한(자동 모드 분류기)에 막혀 **미실행**. 사용자가 직접 삭제 필요.
- 다음 세션 첫 행동: 0단계 docs/00-strategy.md(전략 정의 형식) 초안 작성.

## 2026-10-01 (3) — Git 사용자 설정, 0단계 초안
- 수행: 저장소 로컬 Git 사용자 설정(user.name/user.email, 사용자 지시). 0단계 `docs/00-strategy.md` v0.1 draft 작성(전략 정의 형식, 고정 코어·확장 지점(능력 CAP) 방식, 등록 명세, 신호 형식, 상태 전환, 부록 A 양식). docs/README.md·.status 현재 상태 갱신(게이트는 미승인 유지).
- 대화 합의: 매매전략은 추후 결정. 설계는 전략과 무관한 고정 코어 + 확장 지점으로 하고, 맞지 않는 전략은 기능 추가(CP)로 대응.
- 변경 파일: docs/00-strategy.md(신규), docs/README.md, docs/.status, docs/worklog.md. `.git/config`(추적 안 됨)
- 실행 명령: `git config user.name/user.email` → 조회로 값 확인. `git status` → 변경 없음(작업 전). 상위 `C:\AI\trading_system\.git` 없음 확인(삭제된 상태).
- 커밋: 하지 않음(지시 없음).
- 미해결(0단계 차단): Q1 종목 다중 전략 처리, Q2 신호 표현, Q3 레버리지·인버스 ETF/ETN, Q4 규칙 필터 범위, Q5 한도 초과 시 거부/축소.
- 다음 세션 첫 행동: Q1~Q5 답변 반영 → 00-strategy.md 승인 요청.
- (추가) Q1~Q5 사용자 답변: Q1 배타 소유, Q2 행위 신호, Q3 ETF·ETN 모두 허용(레버리지·인버스 포함), Q4 목록 안에서만, Q5 거부 → 00-strategy.md v0.2 draft 반영(D-013·D-014 추가, CAP-INS-04 신설). 새 미확인: 레버리지·인버스 상품 투자자 요건과 모의 적용 여부, 배수 노출 계산(1단계).
- 다음 세션 첫 행동: 00-strategy.md v0.2 승인 여부 확인 → 승인 시 approvals.md 전사, 부록 A를 templates로 분리, 1단계 착수.
- (추가) 외부 검토 의견 10건(A-4, A-5, B 8건) 검토 → 모두 타당, v0.3 draft 반영: 소유권 게이트 승인 시 예약·체결 0주 종결 시 해제, 버전 교체 절차, D-009 위험 확대에만 적용, 위험 축소 우선순위 고정, 초안에서 백테스트 허용, 절대 상한 기한, 미체결 정책 파라미터화, 거래 이력 입력, 판정 ID(이벤트 식별자), docs/capabilities.md 지정. "CP 미정의"는 CLAUDE.md §2에 정의되어 있어 참조만 추가.
- (추가) 사용자: 미확인 항목이 나중에 확인되는 것이면 00-strategy v0.3 승인 → 모든 미확인의 필요 시점이 1단계 이후임을 확인, 승인 전사(A-005). 00-strategy.md approved, .status 현재 단계 1·strategy 게이트 approved. 승인 후 작업: docs/templates/strategy-template.md, docs/capabilities.md(전 항목 미지원) 생성, docs/strategies/README.md·docs/README.md 갱신.
- 다음 세션 첫 행동: 1단계 docs/01-requirements.md 착수 — U-01~U-05, U-09, U-11, 레버리지 노출 계산, 위험 축소 신호 허용 조건, 정지 정책 표(교체 중지 포함) 질문 정리.
- (추가) 1단계 착수: docs/01-requirements.md v0.1 draft 작성(요청 유형 6종, 게이트 검사 G1~G15 유형별 적용, 한도 L1~L10 빈칸, 손실률 정의, 정지 유형 K1~K10 기본안, 장애 정책, 거래 상태 정책, 운영 요청·알림, 재시작 복구, R-01~R-28 상세, D-015~D-023). 1차 질문 Q-A~Q-E 대기. 한도 값은 2차 질문.
- (추가) 1차 질문 Q-A~Q-E 모두 권장안 선택 → 01-requirements.md v0.2 draft 반영(D-024 알림 장애 시 위험 확대 차단·K11, D-025 메신저 봇 원격 수단). 권장안이 없던 K6 위험 축소 범위·원격 재개 범위는 2차 Q-H로 이월. 2차 질문 Q-F~Q-I 대기.
- (추가) 2차 답변: Q-F·Q-G는 "실제 매매 시 선택 가능하게" 요청 → D-026 한도·시간 설정값화(기본값 없음, 논리 제약, 엄격 변경 즉시·완화 변경 다음 기준 시각, 원격 변경 불가, LIVE 승인값 초과 거부) 제안 반영. Q-H 권장, Q-I 수용. 01-requirements.md v0.3 draft, 승인 대기.
- (추가) 사용자: 01-requirements v0.3 승인 → A-006 전사, 문서 approved, .status 현재 단계 2·requirements 게이트 approved.
- 다음 세션 첫 행동: 2단계 docs/02-data-model.md 착수 (엔티티 경계, 상태 전이, 멱등키, 가상 전략 2개 적용, 부분 체결·중복·응답 유실·재시작·외부 수동 거래·거래정지 검증).
- (추가) 사용자 지시로 커밋·push (A-007). Git 작성자: 저장소 로컬 설정(YONGKOOLEE).
