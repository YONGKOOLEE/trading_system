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
- (추가) 2단계 착수: docs/02-data-model.md v0.1 draft 작성(ENT-01~29, 멱등키, ST-01~07, INV-01~16, 가상 전략 2개 적용, 상황별 검증 15건, D-027~D-036). 질문 Q-1~Q-3(D-029 종목·방향당 활성 의도 1개, D-028 미전송 재전송 없음, D-034 수동 소유) 대기.
- (추가) Q-1~Q-3 권장안 → 02-data-model.md v0.2 draft. 사용자가 미확인 항목 확인 방법 질의 → 확인 경로(공식 문서/모의 실측/문의/사용자 판단)와 3단계 통과 기준 제안을 답변.
- (추가) 사용자: 02-data-model v0.2 승인 → A-008 전사(A-007은 커밋 지시), 문서 approved, .status 현재 단계 3.
- 다음 세션 첫 행동: 3단계 착수 — 키움 REST API·KRX 공식 문서 확인(docs/broker-specs/kiwoom-kr.md), 3단계 통과 기준(확인됨 또는 보수적 대안+PAPER 검증 등록) 제안, 03-design.md 초안.
- (추가) 사용자 지시로 커밋·push (A-009), 이어서 3단계 착수 지시(누락 없이 수행).

## 2026-10-01 (4) — 3단계 착수: 공식 문서 확인, 설계 초안
- 수행: 키움 REST API 공식 자료(포털 API 가이드·소개, 공식 GitHub 명세 JSON 커밋 953e5db) 열람·추출, KRX 규정 페이지·영문 가이드 열람, PyPI 메타데이터(requests·websockets·pytest·hypothesis·httpx·tzdata) 조회, 텔레그램 Bot API 문서 열람. docs/broker-specs/kiwoom-kr.md, krx-market.md, docs/03-design.md v0.1 draft 작성(D-037~D-066, PV-01~12, CP-011~013 제안).
- 실행 명령: curl로 공식 GitHub 파일을 scratchpad(저장소 밖)에 내려받아 python으로 필드 추출. `python -c sqlite3` → 3.50.4, `zoneinfo.ZoneInfo('Asia/Seoul')` → 실패(시간대 데이터 없음). 설치·API 호출·로그인 없음. PDF 렌더 도구(poppler) 없음 → 표준 라이브러리로 텍스트 추출.
- 주요 발견: 고객 주문 식별자 없음, 정정·취소 시 새 주문번호, 모의 호출 제한 TR당 1초 1회, 토큰 IP 바인딩(8010), 공식 클라이언트는 인증 실패 시 요청 자동 재전송(주문 경로 사용 금지), 조회·실시간 응답에 계좌번호 포함, KRX 호가단위 출처 간 불일치.
- 라이선스: 키움 명세는 복제·배포 금지 → 저장소에 원문 미포함, 사실만 요약.
- 미해결: Q-1~Q-5(03 §16), 사용자 확인 K-U1·K-U4·X-U3.
- 다음 세션 첫 행동: Q-1~Q-5 답변 반영 → 03-design 승인 요청 → 승인 시 CP-011~013 반영(승인본 archive 후 새 버전).
- (추가) Q-1~Q-5 권장안 → 03-design.md v0.2 draft. 사용자가 Claude 계정 전환 시 세션 유지 여부 질의.
- (추가) 사용자 지시로 커밋·push (A-010). 03-design은 draft 상태로 커밋.
- (추가) 사용자: 03-design v0.2 승인 → A-011 전사, 03 approved, .status 현재 단계 4. 승인 범위에 포함된 CP 반영: 승인본을 archive에 보관(02 v0.2, 01 v0.3, 업무정의서 v0.4 — 아직 유효한 승인본으로 표기) 후 새 초안 작성(02 v0.3 CP-011, 01 v0.4 CP-013, 업무정의서 v0.5 CP-012). .gitattributes 생성(D-063).
- 다음 세션 첫 행동: CP 반영 초안 3건 승인 확인 → 4단계 04-validation.md 착수.
- (추가) 사용자: CP 반영 초안 3건 승인 → A-012 전사. 02 v0.3·01 v0.4·업무정의서 v0.5 approved, 이전 승인본 superseded 표기.
- (추가) 사용자 지시로 커밋·push (A-013), 이어서 4단계 착수 지시.
- (추가) 4단계 착수: docs/04-validation.md v0.1 draft 작성(D-067~D-077, 백테스트 공통 틀, 실행 안전성 V-01~V-111, PAPER-0/PAPER-1, PV 확인 절차, LIVE 체크리스트 C-01~14, R-ID·E-ID 추적). 질문 Q-1~Q-5 대기.
- (추가) Q-1 수정안(공통 기준+전략별 기준, 거래 1건=왕복) + Q-2~Q-5 권장 → 04-validation.md v0.2 draft. CP-014(strategy-template 항목 추가) 제안.
- (추가) 사용자: 04-validation v0.2 승인 → A-014 전사, .status 현재 단계 5. CP-014: 양식 보관(archive/strategy-template-A005.md) 후 백테스트 통과 기준·PAPER-1 전략별 기준 항목 추가(draft, 승인 대기).
- (추가) 사용자: CP-014 양식 승인(A-015), 커밋·push 지시(A-016), 5단계 착수 지시.
- (추가) 5단계 착수: docs/05-plan.md v0.1 draft 작성(T-000~T-019, D-079~D-084, 추적표, 착수 차단 조건, 커밋 정책·설치 계획 제안). 질문 Q-1~Q-4 대기.
- (추가) Q-1~Q-4 권장안 → 05-plan.md v0.2 draft. D-087: 작업 착수 직전 사용자 준비·승인 항목 재안내(사용자 요청).
- (추가) 사용자: 05-plan v0.2 승인(A-017) 후 커밋·push(A-018). 0~5단계 게이트 모두 승인. 구현 시작은 미지시. 사용자가 산출물 전체 재검토 요청.
- (추가) 산출물 재검토 결과: ① CP-015(INV-15가 취소·정정 의도를 막는 충돌) → 02 v0.3 보관 후 v0.4 draft, ⑦ CLAUDE.md 커밋 정책(A-017)·폴더 구조 확정(A-011) 문구 갱신. ②~④ 정책 결과는 값 설정 시 재안내, ⑤·⑥은 T-014~T-016에서 처리.
- (추가) 사용자: CP-015 승인(A-019), 커밋·push(A-020).
- (추가) 사용자 지시로 docs/initial-setup → main fast-forward 병합 후 push (A-021). 이후 작업 브랜치 기준은 main.

## 2026-10-01 (5) — 구현 시작, T-000 개발 환경·골격
- 승인: 구현 시작(A-022), 1차 설치(A-023). 브랜치 `feat/T-000-dev-env` (main에서 분기).
- 수행: `.venv` 생성, `requirements-dev.txt`(pytest 9.1.1·iniconfig 2.3.0·packaging 26.3·pluggy 1.6.0·pygments 2.21.0·colorama 0.4.6, PyPI SHA-256 고정), `pyproject.toml`(pytest 설정, paper 마커 기본 제외), 03 §13 패키지 골격(15개 + broker/fake·kiwoom), tests 하위 폴더, 의존 방향 검사기·테스트(V-93), README 3개 갱신.
- 실행 명령과 결과:
  - `python -m venv .venv`; `pip install --require-hashes -r requirements-dev.txt` → 6개 설치 성공. `pytest --version` → 9.1.1 (Python 3.14.7)
  - 골격 생성 전 `pytest -q` → 1 failed(패키지 15개 없음, 기대한 실패), 23 passed
  - 골격 생성 후 → 24 passed
  - 임시 위반 모듈(strategy → broker, datetime.now) 추가 → 1 failed, 위반 2건 검출 → 모듈 삭제 → 24 passed
- 남은 위험: 03 §7에 규칙이 없는 패키지(evaluation 등 9개)는 app import 금지만 검사. broker 규칙을 fake까지 확장 적용(D-062 근거). V-94(주문 경로 단일성)는 T-001.
- 다음 세션 첫 행동: T-001 착수 전 준비 항목 안내(D-087: 외부 연결·설치 없음) → T-001 테스트 작성.

## 2026-10-01 (6) — T-001 최소 종단 간 흐름
- 브랜치 `feat/T-001-min-e2e` (T-000 미병합이라 `feat/T-000-dev-env` 위에서 분기).
- 구현: domain(clock·enums·models: Decimal 강제, 시간대 강제, 결정적 신호 ID), broker(base 인터페이스, fake: 체결/접수만/거부/전송 전 실패/결과 불명), storage(SQLite, 모드 기록, BEGIN IMMEDIATE 트랜잭션, 감사 추가 전용 트리거, 주문 식별 (계좌,거래일,주문번호)), gate(G1·G2·G3·G4·G6·G7(L2·L3)·G9, 판정·의도·예약·소유권 한 트랜잭션, 직렬화), execution(ApprovedIntent만, KILL 파일, 전송 시도 선기록, 4분류, 통보 반영·포지션·소유권·예약), halt(K1 발동 → 미체결 매수 취소를 게이트 경유). 테스트: 불변 조건 검사기(INV-01·03·04·05·06·07·08·09·10·12·15·17), 시나리오 17건, 주문 경로 단일성(V-94), 검사기 자체 검증.
- 실행 명령과 결과:
  - 구현 전 `pytest -q` → 수집 오류(구현 모듈 없음, 기대한 실패)
  - 구현 후 → 43 passed
  - 변이 시험: G7·G4·G2·G6·G9·KILL 파일 확인을 하나씩 무력화 → 각각 시나리오 실패 발생(G9는 종료 시 INV 오류도 발생)
  - V-04 동시성 30회 반복 → 실패 0
  - 검토 중 결함 발견: 거부된 신호가 재도착하면 한도 여유 시 승인됨(01 D-016 위반) → 재현 테스트 실패 확인 → G4가 같은 멱등키의 기존 판정(승인·거부 무관)을 중복으로 거부하도록 수정 → 48 passed
- 완료 기준 충족: V-01(L2·L3 경계), V-04, V-07, V-12, V-20(K1 부분·KILL 파일), V-26, V-55, V-94, INV 위반 0.
- 남은 위험·후속: G2는 활성 정지가 있으면 위험 확대를 일괄 차단(유형별 세부는 T-005). 예약 금액은 신호 가격 기준(시장가 상한가 기준은 T-003). 체결 소비액은 의도 가격 기준. 결과 불명 해소는 T-004. var/backtest/<실행ID> 실행기는 T-009(앱 배선). 
- 다음 세션 첫 행동: T-002 착수 전 준비 항목 안내(D-087: 설치·외부 연결 없음 예상).

## 2026-10-01 (7) — T-002 설정·모드·비밀
- 브랜치 `feat/T-002-config-mode` (T-000·T-001 미병합이라 `feat/T-001-min-e2e` 위에서 분기). 모델: Sonnet 5.5로 전환 후 수행.
- 구현: domain(clock: `next_kst_time_after`, limits: AccountLimits·논리 제약 검사), config(errors, schema: 필드 검증·float 거부·문제 일괄 보고, envfile: Secret·SecretStore, limits_file: TOML 로더, modes: TRADE_MODE, versioning: 방향 판정·적용 시점·원격 거부·감사), app(startup: 모드·저장소·자격정보 일치, LIVE 거부), storage(settings_version 표와 조회), 예제(`config/limits.example.toml` 자리표시자, `.env.example` 갱신), 불변 조건 INV-14 검사기.
- 실행 명령과 결과:
  - 구현 전 `pytest -q` → 수집 오류 6건(모듈 없음, 기대한 실패)
  - 구현 후 → 244 passed, 1 failed: 범위 오류가 누락 오류와 한 번에 보고되지 않음(설계 의도: 문제 일괄 보고) → 범위 검사를 필드 스키마로 이동(테스트 수정 없음) → 245 passed, 0 failed, 0 skipped
  - 변이 시험 11종(완화 즉시 적용, 원격 차단 해제, 대기 중 완화가 덮는 순서, 현재값 대신 최신값과 비교, LIVE 허용, 다른 모드 키 허용, 저장소 경로 검사 제거, 모드 문자열 관대화, 비밀 값 노출, float 허용, 기준 시각 유예 제거) → 전부 테스트 실패로 검출
- 완료 기준: V-100·V-101·V-102·V-86(일반 스키마, 전략별 스키마는 T-007)·V-87(규칙 단위, 채널 연동은 T-013)·V-109 통과, 불변 조건 위반 0.
- 결정·주의:
  - 방향 판정은 **현재 적용 중인 값**과 비교(대기 중 완화를 거쳐 즉시 적용을 얻는 우회 차단). 나중 제출이 대기 중인 이전 버전을 덮음(강화 뒤 완화가 되살아나지 않음). 현재 적용값과 같은 값을 다시 내면 대기 중인 완화를 취소.
  - ENT-08 방향 열거에 없는 NEUTRAL(위험 값 변경 없음: 보고 시각 등)을 내부 값으로 추가. 02 개정 필요 시 CP 제안(미제안).
  - 설정 파일의 시각은 시장 현지 시각(KST)의 시각-of-day. 다음 기준 시각 계산은 휴장·주말을 고려하지 않음(T-010).
  - L1~L10 범위·순서는 01 §2.3.1 그대로. 100% 초과는 오류가 아니라 경고.
  - 게이트(T-001)는 아직 SettingsManager를 쓰지 않음 → T-003에서 `effective_limits(...).to_gate_limits(기준 금액)` 연결.
  - 리스크 수치는 저장소에 두지 않음. 테스트의 값은 가상.
- 미해결: 없음(차단 항목 없음).
- 다음 세션 첫 행동: T-003 착수 전 준비 항목 안내(D-087: 설치·외부 연결 없음 예상) → 게이트 완성(G1~G15, 요청 유형 6종).

## 2026-10-02 (8) — 전체 검토, 견고성 수정, 컨텍스트 분할
- 요청: 요구사항-설계-검증 빈 곳, 비정상 입력에서 깨지는 곳, 근거 없는 가정 검토. 결과: `docs/changes/CP-016-review-2026-10-02.md`.
- 코드 수정(브랜치 `fix/review-2026-10-02`): DB 모드 고정, 저장소 질의 잠금, 게이트 G3(유형-방향·위험 확대는 신호만·소유자)·G10(수량·가격)·G11(시세 없음), 실행기 전송 후 기록 실패 차단, 비정상 통보 격리, 미처리 통보 보존.
- 명령·결과: 재현 테스트 수정 전 17 failed·2 errors·1 passed(B-7은 기존에도 안전) → 수정 후 전체 263 passed. 변이 시험 7종 모두 검출. B-7·B-8 5회 반복 통과.
- 문서 변경(03 D-088~D-090, 04 V-112~V-115·V-01 정정, 02 ENT-08 NEUTRAL 등)은 CP-016으로 제안만, 승인 대기.
- 컨텍스트 분할: `docs/handoff.md` 신설(다음 세션은 이 파일과 지정 절만 읽음).
- 다음 세션 첫 행동: CP-016 승인 여부 확인 → T-003.
- (추가) 사용자 '전부 승인': CP-016 반영(02 v0.5, 03 v0.3 D-088~D-091, 04 v0.3 V-112~V-115·V-01 정정, krx-market X-U4·X-U5, .env.example 안내), B-10 수용(A-024), 커밋(A-025, push 없음). 다음: /clear 후 'T-003 진행'.

## 2026-10-02 (9) — 묶음 A 무인 진행 (T-003~T-006)
- 지시: "묶음 A 무인 진행, 결정은 권장대로", 끝나면 30분 뒤 PC 종료 (A-026 전사).
- 커밋(로컬, push 없음): T-003 게이트 완성 5ba9866, T-004 주문 상태·결과 불명 53c2041, T-005 정지·운영 명령 5d1386a, T-006 장애 주입·무작위 8c60819.
- 명령·결과: `.venv\Scripts\python -m pytest -q` 263 → 338 → 361 → 398 → 407 passed (0 failed, 0 skipped). 변이 시험: T-003 21종, T-004 17종, T-005 21종, T-006 6종 모두 검출(처음 미검출 2종은 테스트 추가 후 검출). 다중 프로세스(scripts/kill) 5회 반복 통과.
- 발견·수정한 결함: 결과 불명 취소가 늦게 해소되면 취소 의도가 열린 채 남음(T-004), 종결된 주문에 늦은 체결이 오면 주문이 다시 열림(T-006 무작위·지정 시나리오).
- 테스트 변경: T-001 V-20의 "K1 중 전략 청산 허용" → 거부(01 §2.6 K1 표대로), T-003 임시 정정 테스트 → V-48로 대체. 사유는 커밋 본문.
- 문서(미커밋): CP-017 draft(해석·제안, §5 확인 요청 Q1~Q5), handoff, approvals A-026, .status.
- 미해결: CP-017 승인, 시세 신선도 값. 차단 조건 없음(묶음 B 착수 가능, 단 Q1 답에 따라 K1 규칙 수정 가능).
- 다음 세션 첫 행동: CP-017 §5 답 확인 → 반영 → 묶음 B(T-007) 착수 전 D-087 안내.

## 2026-10-02 (10) — CP-017 승인 반영, Q3 권장값, 2차 전체 재검토
- 사용자 "전부 승인": A-027 전사, 02 v0.6·03 v0.4·04 v0.4·05 v0.3 (승인본 archive, superseded 표기), .status 갱신.
- Q3 시세 신선도: 권장 60초(조회 응답 수신 시각 기준), 근거·확인 방법은 CP-018 §1. 확정은 사용자.
- 재검토: 문서·코드·구조 세 갈래(서브에이전트 병렬, 결과는 직접 확인). 코드 결함 11건 + 구조 결함 4건 수정 → 3658c51 (브랜치 fix/review-2026-10-02b).
- 명령·결과: pytest 407 → 429 passed. 회귀 22건(수정 전 17 실패), 변이 17종 검출, 재현 스크립트 7종 확인.
- 제안·결정 요청: CP-018 draft (§3 Q1~Q5 차단, §4 D-1~D-19). 가장 큰 공백: 앱 배선·키움 시장 데이터 작업이 계획에 없음(D-1), PAPER-0 검증 전략 진입 경로(Q2).
- 다음 세션 첫 행동: CP-018 답 확인 → 반영(문서 개정, 05 작업 추가) → 묶음 B.

## 2026-10-02~03 (11) — A-030 무인 진행: CP-018 반영 → 묶음 B → 묶음 C(오프라인) → 검토 수정 → T-014 → T-021 → T-020
- 지시: "전부 허용 무조건 완결성과 완벽한 프로그램을 위해 동작해"(A-030), 이후 "다음 진행사항 진행 후 끝나면 pc 종료".
- 커밋(로컬, push·main 병합 없음, 브랜치 사슬): a89cb15 문서 CP-017/018 → 7c1ec40 준비 → a9538ed T-007 → 94328bb T-008 →
  186c24b T-010 → 1e6fd64 T-009 → 7a5cdf4 T-011 → 7ff1198 T-012 → bfd896b T-013 → b4c7c98 검토 B 수정 15건 →
  1362ff2 T-014 → a77f7d4 T-021 → 5d9d8f8 T-020 (현재 브랜치 `feat/T-020-app`).
- 명령·결과: `.venv\Scripts\python -m pytest -q` 600(bfd896b) → 620(b4c7c98) → 669(T-014) → 676(T-021) → 700 passed(T-020), 실패·건너뜀 0.
  변이 시험: 검토 B 29종, T-014 38종, T-021 16종, T-020 28종 — 처음 미검출은 시험 보강 후 모두 검출.
- 커밋 메시지 정정: 7c1ec40 본문 "435 passed" → 실제 434, 7a5cdf4 본문 "571 passed" → 실제 570 (이후 커밋은 실행 결과를 그대로 기록).
- 외부 열람: 키움 공식 GitHub 공개 명세 JSON·클라이언트 코드·예제(로그인·API 호출 없음) → broker-specs §7a, K-U14·16·17·18. 키움·텔레그램·SNTP 실제 접속 없음.
- 발견·수정한 결함(주요): 외부 주문 인정 거래일·수량 차이 덮어쓰기·같은 버전 재등록·복구 판정 SENT 누락·오래된 일봉·
  마스킹 누락·폐기 K9·누락 체결 거부 시 해소 기록·열린 항목 남은 일치·';' 분할 마이그레이션·UTC 로그 날짜(B-1~B-15),
  전각 숫자 통과(엄격 파서), 엔진에서 전략에 미검증 파라미터 전달.
- 시험 변경(요구 변경): T-007 v83(보유 남은 전략 폐기 거부), T-011 마이그레이션(스키마 2 기준). 사유는 커밋 본문.
- 결정 대기 DP-10~DP-26 추가(모두 비차단, 보수적 임시값). capabilities M1 일부 "검증 중"(DP-24).
- 미해결: 결정 대기 일괄 검토, CP 필요 1건(DP-23 진입점 이름), PAPER 시작 전 사용자 확인(K-U1 IP·단말기, K-U6 거래정지 표시, X-U1 호가 표, X-U3 휴장일, 수수료 K-U9).
- 다음 세션 첫 행동: handoff.md 확인 → 사용자 결정 대기열 검토 → T-015(키움 모의 접속, 조회만) 승인 여부.

## 2026-10-03 (12) — 결정 대기 DP-1~26 확정(A-031), DP-5 변경 반영
- 사용자 확인: DP-1~4·6~26 임시 결정 확정, DP-5 변경(일봉 확정 시각 = max(정규장 마감, 20:00), 애프터마켓 고려).
- 변경: `marketdata/store.py`(DAILY_BAR_FINAL·daily_bar_final_at), `marketdata/kiwoom_feed.py`(당일 봉 저장 기준 동일화),
  시험 2개(test_t010 V-90, test_t021 당일 봉) — 15:30~20:00 당일 봉 제외로 기대값 변경(요구 변경 A-031).
  문서: approvals A-031, decisions-pending 상태 "확정", kiwoom-kr K-U19(ka10081 KRX/통합 범위, PAPER-0 확인).
- 명령·결과: 변경 시험 먼저 실행 → 2 failed(예상), 구현 후 `.venv\Scripts\python -m pytest -q` → 700 passed. 브랜치 `fix/dp-5-daily-bar-cutoff`(feat/T-020-app에서 분기).
- 발견: 텔레그램(ops/telegram)이 앱 실행 경로(app/main)에 연결되지 않았고 `.env` 키 이름도 없음 → T-013 실제 송수신 확인 불가. 보완 작업 승인 필요.
- 미결: DP-2(04 V-80 문구 CP 여부), DP-25(03 D-092와 차이 정리 방식), DP-24/DP-1과 PAPER-0 순서(T-016 때).
- 다음 세션 첫 행동: 사용자 T-015 준비(앱키·IP·.env.paper·config paper 3종) 확인 → T-015 승인 여부, 텔레그램 연결 보완 승인 여부.
- (같은 날 추가) A-032: DP-2·DP-25는 CP로 문서를 실제 동작에 맞추는 방향. CP-019 draft 작성(03 D-092, 04 V-80, 05 T-020 진입점) — 반영은 CP-019 승인 후.
- 사용자가 대화에 실제 모의 자격정보를 붙여넣음(유형: 앱키·시크릿·계좌번호, 값 미기록) → 재발급 권고. `.env.paper`는 저장소에 아직 없음(키 존재만 점검, 값 미열람).
- (같은 날 추가) CP-019 승인 A-033 → 03 v0.6·04 v0.6·05 v0.5 반영, 승인본 archive(superseded 표시), .status 갱신.
  PAPER 설정 A-034: `config/limits.paper.toml`·`market.paper.toml` 작성, 프로젝트 로더로 검증 통과(경고 0). 비용 요율 DP-27 보수 임시값.
  `.env.paper_virtual` → `.env.paper` 이름 변경(키 4개 존재·형식만 점검, 값 미열람, git 제외 확인). 텔레그램 연결은 사용자 "나중에".
  시험 미실행(코드 변경 없음).

## 2026-10-03 (13) — T-015 시작 점검 구현(오프라인), 권한 설정
- 승인: A-035 T-015(키움 모의 연결, 조회만), K-U1 IP 등록 사용자 보고.
- 구현: `app/preflight.py`(GuardedHttp: 주문 API 전송 전 차단·호출 분류 기록 / run_preflight: 토큰 발급 → 조회 8종 1회씩 → 시작 대조 → 토큰 폐기 / preflight_main: 연결 전 거부), `scripts/preflight.py`, `config/app.paper.toml`(connection_approved=false).
- 시험: `test_t015_preflight.py` 18건(구현 전 수집 오류 확인). 돌연변이 6종 중 2종 미검출 → 시험 보강 후 6종 검출. `.venv\Scripts\python -m pytest -q` → 718 passed. 커밋 `feat(T-015)` (브랜치 feat/T-015-preflight).
- 권한: `.claude/settings.local.json`(git 제외) — PowerShell·Edit·Write 허용, push·reset·rebase·merge·clean·pip install·.env 읽기/수정 차단. Remove-Item 차단 줄은 미반영(설정 파일 수정은 매번 확인 필요).
- 미실행: 실제 키움 모의 접속(PV-06·PV-11). 사용자: `.env.paper`에 `TRADE_MODE=PAPER` 추가, `connection_approved = true`.
- 다음 세션 첫 행동: 사용자 설정 확인 → `scripts/preflight.py` 실행 → 결과를 kiwoom-kr K-U8·PV-06·PV-11에 기록.

## 2026-10-03 (14) — T-015 실제 키움 모의 시작 점검 (조회만, 주문 0건)
- 사용자: `.env.paper` TRADE_MODE 추가, `connection_approved = true`. 실행 `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` 3회.
- 1·2회 실패: kt00005 모의 미제공(return_code 20 + RC9000, 8104 아님), ka10054 1511(min_trde_qty 필수). 2회차 전 실패 응답 서버 문구(가림 처리) 기록 기능 추가.
- 수정: kt00001로 교체(`adapter.query_cash`), RC9000 = 8104 취급(`errors.specific_code`), ka10054 범위값 "0"(`kiwoom_feed`). 시험 서버도 실측대로 엄격화(kt00005 RC9000, ka10054 필수값). 공식 명세 JSON(S2) 내려받아 필드 확인(스크래치, 저장소 미포함).
- 3회: **통과** — 토큰 발급·폐기, 조회 8종 정상, 시작 대조 MATCH, 주문 API 0건. 경고: 달력 없음(일봉 품질 거짓 경고 포함), SNTP 미점검.
- 시험: `.venv\Scripts\python -m pytest -q` → 721 passed.
- 문서: kiwoom-kr §7b 실측·K-U1·K-U8 해소, CP-020 draft(01·03의 kt00005 문구), DP-28(qry_tp 3).
- 미결: CP-020 승인, PV-11 만료 전 갱신 관찰(장시간 실행 필요, PAPER-0), 달력 2024~2026(X-U3).

## 2026-10-03 (15) — CP-020 반영, T-016 준비(A-037: 종목·DP-24 a·달력)
- CP-020 승인(A-036) → 01 v0.6, 03 v0.7. A-037: PAPER-0 종목 005930, DP-24 해법 (a), 달력 자동 추출(LIVE 전 재확인 조건).
- DP-24 (a): `strategy_registry` verification_approval — PAPER-0 검증 전략만 "검증 중" 허용, 범위 강제(M1·S-4·U-A·시장가 능력·L7 필수). 시험 16건.
- 달력: `marketdata/calendar_derive.py`, `app/calendar_tool.py`, `scripts/derive-calendar.py`(키움 일봉 2종목 교차, 조회만). preflight 연결 검사를 `connect_paper`로 분리. 시험 5건.
  실행 결과: 2024 18일, 2025 19일, 2026 13일(~10-02), 한 종목만 빠진 날 0일. 2026 남은 휴장(10-05·10-09·12-25·12-31)은 토스증권 공지(웹 검색), 개장 시간 변경 2025-11-13·2026-01-02(보도), 2026-11-19(예상값).
  `config/calendar/krx-2024~2026.toml` 작성 → 시작 점검 재실행 통과, 달력·일봉 품질 경고 사라짐.
- 시험 변경(요구 변경 A-037): test_t010 `test_calendar_dir_ignores_examples`를 임시 디렉터리로. `.venv\Scripts\python -m pytest -q` → 743 passed.

## 2026-10-03 (16) — T-016 준비 진행 (A-038 비용, A-039 지시)
- 비용 확정 A-038: 수수료 0.015%, 매도 세금 0.20%. 백업 PC 안만, K-U6 권장안(A-039).
- K-U6 실측(ka10099 5,834행 집계, 조회만): 거래정지 state 26 vs auditInfo 112 → `master_statuses`가 auditInfo도 사용(DP-29). 시험 보강.
- 복원 시험: `scripts\backup.py --mode PAPER` → `scripts\restore-test.py --mode PAPER` 통과(무결성·모드·스키마 2·감사 연속·추가 전용).
- 구현: `strategy/paper0_verify.py`(09:30 1주 시장가 진입, 14:30 청산), `config/strategies/paper0-verify-v1.toml`, 배포 설정 `verification_approval`(PAPER만), `app/strategy_tool.py`+`scripts/strategy.py`(show/register/transition), `repository.strategy_states`. 시험 16건.
- 백테스트: 모의 일봉 005930 CSV(조회만) → 1천만 원 실행은 L1으로 전부 거부(설계대로) → 1억 원(모의 예수금) 재실행 통과: 212 거래, 재현성·룩어헤드 통과, G12 2건(개장 10:00일). DP-30.
- PAPER 저장소 등록: `scripts\strategy.py register --mode PAPER` → paper0-verify:1 초안.
- 문서: docs/strategies/paper0-verify/{strategy,backtest-v1,status,pv-procedure}.md, kiwoom-kr K-U6.
- 시험: `.venv\Scripts\python -m pytest -q` → 759 passed.
- 남은 사용자 항목: 전략 전환 승인 A-ID 2개(BACKTEST_PASSED·PAPER), SNTP 연결 승인, U-06 약관. 그 뒤 PAPER-0 실행(사용자).

## 2026-10-03 (17) — 세션 종료 인계
- 사용자에게 전체 로드맵(PAPER-0 → T-017 → T-018 전략 결정 → PAPER-1 → LIVE 별도 승인)과 LIVE 전 필수 항목 안내.
- handoff.md 전면 갱신, .status 현재 단계 갱신. 코드 변경 없음, 시험 미실행(문서만). 마지막 시험 759 passed.
- 다음 세션 첫 행동: 승인 3건 확인 → 전략 전환 → PAPER-0 실행 안내.

## 2026-10-03 (18) — 승인 3건, PAPER-0 전 전체 검토·수정 (A-040~A-043, 무인 2시간 20분)
- 승인 전사 A-040·A-041(paper0-verify v1 → BACKTEST_PASSED → PAPER, `scripts\strategy.py transition` 실행), A-042(SNTP, 1회 점검 bora -0.076s, kriss 1차 무응답·재시도 -0.077s; time2.kriss.re.kr도 조회함 — 승인 목록 밖), A-043(수정 범위·무인 작업).
- 검토: 읽기 전용 에이전트 5개 → RV-01~25(docs/changes/review-2026-10-03-pre-paper0.md). RV-24·25는 01 승인 문구와 구현 불일치라 CP-021 대신 구현 정정.
- 수정: 에이전트 4개 병렬(파일 소유 분리) + 리드 통합. 재검토 2회·e2e(다일 11, 장애 11)에서 추가 결함 F1(차단)~F9, 재검토2 #1~#4, 장애 결함 2건 수정.
- 커밋(feat/T-016-fix, 로컬): a7c3298, 304e538, 62d1ff7, 63c94d0, c019731, a9a970d, 472a42b, 847b669.
- 시험: 시작 759 passed → 최종 `.venv\Scripts\python -m pytest -q` 890 passed 2회 연속(228s·229s). 시뮬레이터 응답 유실 e2e 간헐 실패(수정 전 전체 실행 2/4) → 재조회 대기 추가 후 2/2 통과, 재발 관찰.
- 임시 결정 DP-31~37, 새 미확인 K-U20(계좌번호 형식)·K-U21(빈 결과 목록 키).
- 설정·외부: SNTP 조회 외 브로커·네트워크 연결 없음. .claude/settings.local.json에 Bash 자동 허용(사용자 지시) + 차단 보강(git clean, pip install, .env).
- 다음: 사용자 확인(DP-31~37, 모의 예수금 ≥ 상한가×200, U-06) → main 병합 여부 → PAPER-0 실행(사용자, pv-procedure v0.2).

## 2026-10-03 (19) — A-044 무인 작업 (17:15~, 에이전트 없음)
- 1 결정표 `docs/decision-sheet.md`(차단 Q-1 예수금·Q-2 U-06·Q-3 병합, DP-28~37 권장안). 2 PAPER 집계 도구 `scripts\paper-review.py`(읽기 전용, reporting/paper_review·app/review_tool, 시험 5).
- 3 전략 후보 3종(제안·미등록, strategy/candidates, 시험 7) 오프라인 백테스트(모의 일봉 CSV 005930, 2024-07~2026-10, 슬리피지 100원): 추세 +2.41%·돌파 +0.87%·평균회귀 -0.05%(자금 대비, 투입 ≈0.8%, L7 100만 원 상한가 기준). 005930 보유 +237% — 국면 의존. 문서 `docs/strategies/candidates-2026-10-03.md`(S-Q1~4). 첫 실행은 sizing 표기·L7 위반으로 거부 → 수정 후 r3 실행(이전 실행 폴더 var/backtest/cand-*-v1-all, -r2는 남김).
- 4 LIVE 사전 조사 `docs/broker-specs/live-prep-2026-10-03.md`: 거래세 시행령 제36001호(공식), 토큰 24시간(공식), 실전 호출 제한·수수료·호가 단위는 2차 출처뿐 → 미해소 유지. 웹 검색·조회만.
- 시험: 각 단계 전체 `pytest -q` → 895, 902 passed. 새 시험은 구현 뒤 작성(실패 선확인 없음).
- 커밋(feat/T-017-prep): 1acb106, 6ec01a5, ae8b4c1, 08ff59b, a9fa385, 마지막 문서 커밋.
- 다음: 결정표 답 → main 병합 여부 → PAPER-0(사용자). 지시대로 작업 후 PC 종료.

## 2026-10-04 (20) — A-045 무인 작업 (08:46~, 에이전트 병렬)
- (1) 실제 run_main 리허설(로컬 키움 시뮬레이터) 9건: 시작·잠금·K8 재개·주문·Ctrl+C·재시작·KILL·WS 끊김·SNTP 실패·3.5분 장시간·실행 중 K8. status 미체결 집계 결함 1건 수정.
- (2) 장애 시험 보강 13건 → 03 §6.4 마감 후 대조 미구현 2건 발견·구현(DP-40). (5) 백테스트 하루 여러 판정·틱 내림·V-91 상태 교란(DP-42).
- (3)(4) 독립 검토 3~7차(각각 결함 발견, 일부 직전 수정의 회귀) → 정지 취소 재시도·KILL/RESUME·실행 중 K8·결과 불명 vs 대조·K10 큐·WS 생존 감시·인덱스(DP-38) 등 수정. 규칙 요약 DP-43. CP-021 draft(01 §2.5 문구).
- 커밋 feat/T-017-verify: faace22 … 64b7a8a (로컬, push·병합 없음).
- 시험: 시작 902 → 최종(64b7a8a) 리허설 포함 `pytest -q` 978 passed (746s), 직전 코드에서도 978 passed (787s).
- 안정성 재실행: 리허설 2파일 + e2e 3파일 44 passed (638s) — 간헐 실패 재발 없음.
- 다음: 결정표(decision-sheet, DP-28~43) 답 → main 병합 → PAPER-0(사용자). 지시대로 끝나면 PC 종료(10:48 예약).

## 2026-10-04 (21) — 결정표 처리 세션 (대화형)
- 사용자 결정 전사: A-046(Q-1 예수금 확인, Q-2 U-06 이용 제한 없음·조회 제한 조건), A-047(main 병합), A-048(DP-43), A-049(DP-28~38·40~42 권장안, DP-31 1일차 후 재검토), A-050(CP-021), A-051(모의 계좌 유지·LIVE 전용 실계좌 추가), A-052(CP-022 = DP-39, W-1 운영 규칙 C-4), A-053(01 v0.7·03 v0.8·04 v0.7).
- 작성: CP-022, 01 v0.7(§2.5 취소 정지 중, §2.7 잔고 불일치 예외·수동 주문 규칙), 03 v0.8(§6.3 보류·자동 종결), 04 v0.7(V-125·V-126), pv-procedure §3.1 수동 주문 주의, kiwoom-kr §2 U-06 행, 승인본 archive 3개.
- 병합: main ← feat/T-017-verify fast-forward(71f4438). 처음 2회는 settings 차단으로 거부 → 사용자 규칙 삭제 후 실행.
- 시험: `pytest -q tests/scenario/test_review3_reconcile.py -k "unknown or candidate or adoption"` → 5 passed. 전체 시험은 미실행(코드 변경 없음).
- 미확인: 일일 호출 상한(40,000)의 모의 적용 여부(PAPER-0 관찰), LIVE 추가 계좌 개설·앱키/IP 단위.
- 다음: main 재병합(지시 시) → PAPER-0 실행(사용자, pv-procedure).

## 2026-10-04 (22) — 예수금 관찰 기록(A-054), 무인 운영 절차, M2 방향
- 구현: kt00001 관찰 필드(entr·d1_entra·d2_entra·ord_alow_amt·pymn_alow_amt) → CashView.observed, 엔진이 값 변경 시 감사 CASH_OBSERVED, paper-review "예수금 관찰" 표. 게이트·현금 계산 불변.
- 시험: 새 시험 5건(tests/scenario/test_cash_observation.py) 먼저 작성 → 4 failed(기대한 이유) → 구현 후 5 passed. 전체 `pytest -q` → 983 passed (757s).
- 문서: pv-procedure §3.1 RV-23 자동화, §3.2 출근 전 시작(무인), decision-sheet C절, A-054.
- 결정: 바이낸스 현물 유지, 숏은 나중(CP). 바이낸스 공식 문서 조사 에이전트 실행(로그인·API 호출 없음).
- 미확인: 06:45 전후 키움 모의 서버 응답, 실시간 하루 넘김 운영.
- 다음: PAPER-0 1일차(10/6, 사용자) → 결과 분석. 텔레그램 연결(별도 브랜치), binance-spot.md 검토.

## 2026-10-04 (23) — 검토 #1 수정, 텔레그램 run 연결 (A-056)
- 독립 검토(63a8694): 차단 없음, 낮음 1건(관찰 기록 실패 → 대조 건너뜀·K8) → 수정 72e543b(회귀 2건 선작성·실패 확인), 전체 985 passed, main 병합.
- 텔레그램: 설정 [telegram] enabled(PAPER, 기본 false), 비밀값 없으면 브로커 접속 전 시작 거부, 알림·수신 스레드(주문 없음, 정지는 큐), 첫 연결 시 지난 알림 SKIPPED, 연결 시험 도구 scripts/telegram-test.py, pv-procedure §3.3.
- 시험: 새 12건(설정·run 거부는 선작성 실패 확인, 시험 도구는 구현 후 작성). 전체 `pytest -q` → 997 passed (757s).
- 사용자: 봇 생성·.env.paper 키 입력, telegram-test 전송 성공 보고.
- 못 함: .env.example 자리표시자(권한 차단). 문서 경로 역슬래시가 제어 문자로 깨진 것 발견·수정(pv-procedure 2줄).
- 다음: 병합(지시 시) → 사용자 enabled 켜기 → 10/5 예행연습(run·K8 재개·텔레그램 알림·/status) → 10/6 PAPER-0.

## 2026-10-04~05 (24) — 텔레그램 켜기, 예행연습, 재검토 (A-057·A-058)
- 4cb8f15: 사용자 텔레그램 켜기(app.paper.toml), .env.example 자리표시자(사용자 작성 — 처음에 실제 값을 넣었다가 커밋 전 정정, 에이전트는 내용 미열람), 리허설 시험 설정 분리, binance-spot v0.2(★ 6행 원문 대조). 전체 `pytest -q` 997 passed(755s, 텔레그램 켜진 설정) 뒤 커밋·main fast-forward.
- 예행연습(사용자, 10-04 23:38~23:50, 휴장 시간): preflight 1차 실패(시계 오차 -1.104초) → Windows 시간 동기화 → 통과(-0.002초). run 시작·K8 재개(DONE)·텔레그램 알림·/status 응답 정상. 주문·판정 0건(경로 미검증). 발견: ka10054(VI 목록) 해석 실패 "종목코드 형식 아님" WARNING, 시작 알림 2회, 봇의 과거 메시지 처리.
- 0d92651: pv-procedure §3.2 아침 시간 동기화 단계.
- 브랜치 feat/T-021-code-diagnostic(T-021 시세 공급자 후속): parse.stock_code 오류에 값 표시(20자) — 시험 선작성·실패 확인.
- 재검토(읽기 전용 에이전트 3): 텔레그램 연결 — 높음 없음, 중간 4(지난 /kill 실행, 영구 실패 알림 K11 고착, 전송 간격·429, 명령 유실). 무인 운영 — 높음 3(WS 재접속 10회 실패 시 K5 고착, 첫 실주문 응답 해석 미관찰, 09:30 누락 시 14:30 역매수), 중간 3(SNTP 1회 실패로 판정 누락, 콘솔 선택 모드 멈춤, 단계 예외 K8). 문서 — 게이트 충돌 없음, 병합 지시 전사 누락(A-057로 보완), handoff·README 등 낡은 문장, 02·03 §13 미반영 항목(CP 묶음 필요).
- 문서 정리: .status 주석, decision-sheet 상태, CP-021·022 제목, pv-procedure v0.3(무인 운영 주의), handoff 갱신.
- 미해결 결정: WS 재접속 계속(01 §2.7·03 D-050 변경 CP), 지난 텔레그램 명령 무시, VI 목록 유니버스만 해석·실시간 해석 오류 알림, DP-40·41·43 문서 반영 CP, 문서 CP 묶음(02 열거값·03 §13·04/05 추적).

## 2026-10-05 (25) — 재검토 결정 1·2·3 구현 (A-059, CP-023)
- main ← feat/T-021-code-diagnostic fast-forward(fde7e65). 작업 브랜치 feat/T-020-unattended-fixes.
- 구현(시험 선작성·실패 확인): WS 재접속 한도 뒤에도 계속(치명·FAILED 1회, 복구 시 K5 해제 경로 유지), 지난 텔레그램 명령 무시(STALE_AFTER 120초, 감사 REMOTE_STALE), VI 목록 우리 종목만 엄격 해석, 실시간 통보 해석 실패 경고(종류별 5분 1회).
- 독립 검토(읽기 전용): 필수 수정 없음, 중간 1(IP·단말기 거부 치명 통보가 60초마다 반복) → 1회 보고로 수정(시험 선작성).
- 시험 완화 1건: test_ws_login_rejected_invalidates_token `== 2` → `>= 2`(재시도 계속, R-09·V-69, CP-023 §4). tests/support/kiwoom_sim.py에 ws_login_reject_msg 추가.
- 문서: CP-023 draft, 01 v0.8·03 v0.9·04 v0.8 draft(승인본 archive, 승인 대기), A-059.
- 시험: 최종 코드 `pytest -q` → 1006 passed (807s).
- 다음: 문서 3종 승인(A-060) · 예행연습 1회 더(사용자) → 10/6 PAPER-0.

## 2026-10-05 (26) — 2차 예행연습 (사용자, 브랜치 feat/T-020-unattended-fixes 코드)
- 사용자 보고: preflight → run → status → resume K8 → status 전부 정상. 텔레그램: 01:04 K8 긴급, 01:04 시작 정보(복구 판정 통과), 01:05 K8 재개 완료, /status 응답(정지 0·KILL 없음·결과 불명 0·대조 불일치 0) — 캡처 확인.
- 확인된 것: 수정분(재접속 계속·지난 명령 무시·경고)이 실제 서버 시작·재개·텔레그램 송수신에서 문제를 일으키지 않음. 지난 명령 무시 안내는 이번엔 오지 않음(남은 오래된 메시지 없음).
- 여전히 미검증: 판정 → 주문 → 체결 → 대조 경로(휴장일이라 주문 0건), 06:45 전후 서버 응답, 장시간 운영.
- 다음: CP-023 문서 3종 승인(A-060) → main 병합 → 10/6(화) 06:43 시간 동기화부터 PAPER-0.

## 2026-10-05 (27) — A-060 문서 승인, M2 영향 조사, 구조 결정(A-061), 새 세션 인계
- A-060: 01 v0.8·03 v0.9·04 v0.8 승인(CP-023), main fast-forward 4e502a5.
- M2 영향 조사(읽기 전용 에이전트): `docs/changes/M2-impact-survey-2026-10-05.md` — 재사용 33·일반화 30·M1 전용 14 파일(판독 분류, 미실행), 신규 약 10. 직전에 에이전트가 "핵심은 그대로"라고 답한 것을 조사 결과로 정정(공통 코드 일반화 필요).
- A-061: M2 구조 = 코드 공유 + 실행 분리. 나머지 결정 9건 미결, 사용자 지시로 설계 문서는 아직 미작성.
- 브랜치 docs/m2-binance-design(문서만, main과 코드 동일). 시험 미실행(문서 변경만).
- 다음: PAPER-0 1일차(10/6) 결과 분석, M2 미결 결정 → 설계 CP.

## 2026-10-05 (28) — M2 결정 10건 + 위임 결정(A-062~A-065), CP-024 설계 초안, 바이낸스 원문 대조
- 결정 전사: A-062(①~⑤ 테스트넷·정지 시장별·순 수령·dust·대조 범위), A-063(⑥⑧⑨⑩ IOC 지정가 매수·시장별 봇·HMAC·테스트넷 시세), A-064(M2 PAPER 한도 값, 에이전트 권장안 사용자 확정), A-065(CP-024 §8 Q1~Q5 — 사용자 "2시간 알아서, 결정은 권장으로" 위임. Q4 시세 지연 청산은 안전 항목 → 복귀 후 재확인 권장).
- 작성: `docs/changes/CP-024-m2-binance-spot-design.md` v0.5 draft(D-096~D-120, V-129~V-161, PV-M2-01~11, T-M2 작업 분해). v0.1 → 독립 재검토 21건(v0.2) → A-065(v0.3) → 원문 대조(v0.4) → 2차 독립 재검토(v0.5: dust 판정이 청산을 막던 문제, 호출 우선순위를 M1 원칙으로, 자정 넘는 결과 불명의 대조 보류 기준 등). **CP-024 미승인.**
- 원문 대조(읽기 전용 에이전트, 공식 GitHub raw 커밋 828ca74b): `docs/broker-specs/binance-spot.md` v0.3 — B-U16~B-U21 추가(B-U17·18·19 해소), -1021·-2010 실행 여부, HTTP 4XX 일괄 거부 불가(409·403), MARKET 명목가 기준가(참조가 우선).
- 작성: `docs/strategies/m2-paper0-verify/strategy.md` 초안(미승인, BTCUSDT 20 USDT, UTC 01:00 매수·06:30 매도, 2차 재검토 반영).
- 코드·승인본 01~05 수정 없음. 시험 미실행(문서만). 바이낸스 API 호출 없음.
- 미해결: CP-024 승인(사용자), 검증 전략 승인, 테스트넷 USDT 잔고·L1(기준 금액 4,000 USDT 이상 필요) 확인.
- 다음: 10/6 PAPER-0 1일차 결과 분석(트랙 A). CP-024 승인 시 업무정의서·00~05 개정본 draft(Q3).

## 2026-10-05 (29) — A-066 CP-024 승인, 승인본 개정 draft 7종
- A-066: 사용자 "전부 승인" → CP-024 v0.5 승인 + A-065 Q4 재확인(m2-paper0-verify 전략은 범위 밖, 미승인). 커밋 42a4573.
- 승인본 보존: docs/archive/ business-definition-v0.6, 00-strategy-v0.4, 01-requirements-v0.8, 02-data-model-v0.7, 03-design-v0.9, 04-validation-v0.8, 05-plan-v0.5.
- 개정 draft(병렬 에이전트 5, 파일 소유 분리): 업무정의서 v0.7, 00 v0.5, 01 v0.9, 02 v0.8, 03 v1.0, 04 v0.9, 05 v0.6 — 모두 미승인. 교차 정합 검토 1회(기계적 오류 4종 수정, 미결 7건은 handoff).
- capabilities.md: 에이전트 개정본이 M1 실행 코드가 읽는 파일이라 원복(git checkout), 개정안은 docs/changes/CP-024-capabilities-draft.md로 보관 — PAPER-0(10/6) 실행본 보호.
- 코드 수정 없음(사용자 "구현해도 좋아"였으나 M2 코드는 A-065 Q1·05 M2판 미승인, M1 코드는 PAPER-0 직전이라 손대지 않음). 시험 미실행(문서만).
- 다음: PAPER-0 1일차 분석, 개정 draft 문서별 승인, handoff "개정 draft 미결" 확인.
