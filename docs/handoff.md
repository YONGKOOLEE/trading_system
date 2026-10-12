# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-08 저녁. **PAPER-0 3일차(10-08) "보는 자동 운영" 성공** — 06:44 K8 자동 재개(7초), 07:53 K5 → 07:58 자동 해제(CP-028 P4), 09:30 매수 267,500 / 14:30 매도 266,250, 마감 대조 MATCH, 15:45 자동 종료·paper-review·백업·요약 정상. 결함 2건(요약 기준 금액 표시, 15:09 하트비트 거짓 경보) 수정 013f2bd 병합. 실제 비용 수수료 1,860 + 세금 532, 손익 −3,642(시스템 −3,652, 10원 올림 차). 마지막 승인 기록 **A-084**. 운영 카드: https://claude.ai/artifact/XXZrAhidXPAFo1hUDRgVdw
- **최신 (2026-10-09 낮)**: 사용자가 집 PC에서 **10-12 무인 전환 준비 완료** — ① Windows 시간 서버 time.kriss.re.kr 변경 ② 루트 `.env`(TRADE_MODE 한 줄, `Set-Content -Encoding ascii`) 생성·확인 ③ `schedule-register.ps1 -DryRun` → 등록 ④ 실행 계정 확인 — 사용자 보고 "전부 정상". 등록 중 결함 2건 발견·수정(2e2f713): PS 5.1 `-File`에서 param 기본값의 `$PSScriptRoot` 빈 값, 루트 `.env` 부재(사용자가 `.env.paper`로 확인했었음 — 자동 운영은 루트 `.env`로 모드 판단). 사용자 계획: PC를 끄고 **10-11(일) 저녁 켜고 로그인·화면 잠금 상태로 둔다**(예약 작업은 로그인 상태에서만 실행).
- **체크아웃 `main`** HEAD **2418489**(정지 요청 파일 삭제 재시도 e1fbb2b 병합 — 2e2f713 전체 회귀에서 test_v208 간헐 실패(1/12 재현) 원인 수정, 브랜치 전체 **1452 passed**) ← 2e2f713(2026-10-09 낮 예약 등록 수정 병합. 이전: 4e76347 2차 병합 `integ/final` 1449 passed — 백테스트 결과 파일·검증 실행기, confirm-unknown, 운영 낮음 항목, 문서; 9a494f1 1차 병합 1335 passed — ops-hardening·code-health·unknown-k3(CP-031)). push 없음. 10-09~11 휴장·주말. **10-12(월) 06:40 시계 맞춤 → 06:45 첫 무인 자동 운영.**
- **최신 (2026-10-12 저녁, 세션 44)**: **10-12 첫 무인 자동 운영 성공**(07 §12C) → A-086 조건 충족 → **M2 main 병합 c2ed1e1(2397 passed)**, main cf9b883. 10-13부터 M1 텔레그램 `[M1]` 접두어·시작 일치 검사. **CP-034 승인(A-092)·Q-6 값(A-093)·구현 완료 main 79933ba(2578 passed)**, 프로필 파일 limits.research.toml·limits.paper1.toml. 남은 질문 CP-034 Q-10(지수 확정 시점). CP-033 draft(수급)는 승인 전.
- **최신 (2026-10-09 밤, 세션 43)**: 사용자 "CP-032 Q-1, Q-2 승인하고 전략실 만들어" → **A-085**. 이어서 "병합 후 진행" → "승인 없이 할 수 있거나 권장인 일 전부 진행". 결과:
  - **main 3ee9f97 (1566 passed)** = T-027 지정가 오프라인 구현 + 실측 도구(kiwoom-probe) + capabilities.md 반영(CAP-ORD-03 → 03a 미지원·03b M1 검증 중, CAP-ORD-02 검증 중 유지 — PAPER는 "지원"만이라 지정가 PAPER 불가 유지) + CP-032 단계 1 draft 반영(00·01·03·04·05·kiwoom-kr·krx-market) + 설명서 v0.3(실측 도구 §11·전략실 §12·일일 기준 기록 손상 부록 A·confirm-unknown 안내). **10-12 무인 운영은 이 main으로 돈다** — 바뀐 점: krx.toml 로드(시세 호가 칸 채워짐), 운영 요청 지정가 거부. 시장가 동작 불변(V-240).
  - **실측 도구 실행은 에이전트 권한 거부**(자동 권한 판정 "Sensitive Remote Exec") → 사용자 실행 대기.
  - **M2 오프라인 작업 전부 완료, 미병합**: `integ/m2-2026-10-09` 9df2fd2 **2293 passed**(10-10 마무리 포함)(아래 표). main 병합은 10-12 성공 뒤(권장, 사용자 지시).
- 실전 주문 코드·LIVE 승인 없음. 게이트 변경 없음(00~05·업무정의서 draft 묶음 승인 전, 검토 요약 docs/changes/draft-bundle-review-2026-10-07.md).

## 브랜치·worktree (2026-10-09 밤)
**미병합 — M2 통합 1개 (10-12 성공 뒤 병합, 사용자 지시)**
| 브랜치 (worktree) | HEAD | 전체 시험 | 상태 |
|---|---|---|---|
| integ/m2-2026-10-09 (`.claude\worktrees\agent-adf1305a61821e485`) | 9df2fd2 | **2293 passed**(에이전트 실행, 커밋 본문 기록; 2f0b277까지 리드 2222 passed) | main 3ee9f97 + M2 T-M2-01·02·03·07·10(재통합) + **T-M2-04·05·06·12·09**(feat/T-M2-04-06 83cf082) + **T-M2-08·11·13**(feat/T-M2-08-11-13 9fe6b1d). 충돌 0. 병합은 `git checkout main` → `git merge --no-ff integ/m2-2026-10-09`(run 꺼진 저녁) |
- **M2 병합 시 M1에 생기는 변화**(사용자에게 미리 알릴 것): ① 텔레그램 보내는 모든 메시지 앞에 `[M1] `(T-M2-13 D-117, 명령 해석·/confirm 불변) ② 시작 일치 표(모드·시장·브로커·도메인·계좌 별칭 접두어 `KR-`) 불일치면 시작 거부 — 현 설정 통과 ③ 새 DB에 market·broker 메타 2행, 기존 M1 DB는 쓰지 않음(기록은 migrate가 백업 뒤 — PAPER-1 중에는 migrate 금지) ④ 운영 도구 출력 첫 줄 `시장: … · 디렉터리: …` ⑤ 새 표 fill_fee·client_order·recon_asset을 DB 열 때 추가(SCHEMA_VERSION "2" 유지, 기존 M1 DB 그대로 열림 시험) ⑥ 시험 지원 불변 조건 검사기 INV-04·06 Decimal 합·남은 수량 기준(02 INV-06·G9 정의).
- **M2 결정 — A-086(2026-10-11) 승인**: 슬리피지 0.1%·결과 불명 기대값(V-41·48·125·126)·추가 전용 표 확정, **main 병합은 10-12 성공 조건부**. 나머지 [제안]은 미승인. (원래 기록) T-M2-12 슬리피지 값(시험은 0.001 임시, 05 §2-M2.4 착수 차단 항목), T-M2-09 기대값(V-41 5회 무응답 K3, V-48 정정 G10 거부·취소 종결 확인, V-125/126 결과 불명 24시간 보류·같은 조건 외부 주문 K4), 스키마를 번호 붙은 전진 마이그레이션(05-plan) 대신 추가 전용 표로 한 것(A-072 ⑤ 방식, 무인 운영 시작 실패 방지), [제안] 다수(B줄기 9건·A줄기 7건 — worklog (43) 참조).
- **M2 남은 것** (2026-10-10 마무리 96cfdf4·9df2fd2): 418 즉시 K5(사람 재개, IP 차단 해제 확인 뒤 대조 일치로만 해제)·-1021 연속 3회 K6 구현(D-113·D-115, 합성 응답). V-157 미실행 24개 중 20개 M2판 시험 추가(감시기·일일 보고 시장별 일 경계 — M1 KST 기본 불변, M2 UTC). **남은 미실행 4개**(V-02·V-76 부분 시험만, V-112·V-127) — 사유는 test_tm2_09_v157.py 대응표(T-M2-14 등). T-M2-14 테스트넷 연결·M2 엔진 조립은 **별도 승인**. M2 유니버스 심볼(D-105) 사용자 결정. 미확인 B-U3·B-U4·B-U13·B-U16·B-U21, referencePrice 형식.
- 남은 위험(기존): paper_review DAILY_BASE 목록 시장 구분 없음(보고용), spot_cash._saved_day 깨진 M1 행 예외, 바이낸스 어댑터 합성 응답 시험만.

**병합 완료 (2026-10-09)**
- 1차(지시 "운영 묶음과 문서 main 병합"): ops-hardening(7e71480·d71ebc1·242476e), code-health(6bac802·f6d5090), unknown-k3(0f6aef6·2109f81), docs/cp-030-validation-criteria, docs/user-manual → 9a494f1, main 1335 passed.
- 3차(사용자 등록 오류 보고, 2026-10-09 낮): fix/schedule-register-root 308f43d → 2e2f713 — schedule-register.ps1 Root 본문 계산·루트 `.env` 모드 줄 사전 검사·`-DryRun`, config/modes UTF-16 모드 파일 → 거부(예외 아님), 설명서 §2.2·§2.3·pv-procedure §3.5. 관련 시험 128 passed. (병합 커밋 메시지에 비밀 파일 이름이 있으면 권한 거부됨 — 메시지에서 이름 빼고 재시도.)
- 2차(지시 "남은 개발 전부 시행"): integ/final = fc2790f(test_r4 대기 보강) + integ/backtest(T-009 295e74d·T-026 03132f7, 충돌 0) + fix/confirm-unknown-cli(15b5ceb·f836f12, 재검토 반영) + docs/manual-v02-cp032 + docs/paper-review-v04(kiwoom-kr.md 충돌 1건 — §7e 다음에 확장 사양 조사 절, 둘 다 보존) + fix/ops-low-items(c7e16da·b2b535e, 재검토 반영) → 4e76347.
- 4차(2026-10-09 밤, 지시 "병합 후 진행"·"전부 진행"): docs/a-085 → 9f67e59, feat/T-027-limit-order → 0e96677, feat/cp032-probe → abb6885(1566 passed) → docs/capabilities-cp032(f73456c capabilities·시험 1건 변경) + docs/cp032-stage1-reflect(a0b82eb) + docs/manual-probe-recovery(66430f0) + 후속 3ee9f97 → main 3ee9f97(1566 passed).
- 정리 대상 worktree(병합 끝남, 사용자가 `git worktree remove ..\<이름>` 실행 — 에이전트 권한 거부): sched-fix·stop-fix·cp028·autopilot·t018-multi·day3-fixes·ops-hardening·code-health·unknown-k3·integ·docs-cp030·docs-manual·bt-export·t026·confirm-cli·ops-low·docs2·docs3·final·m2-t07·m2-binance·m2-adapter·m2-next·integ2(M2 병합 뒤). `.claude\worktrees\` 아래 agent-* 7개(T-027·probe·문서 2·M2 A·B — 병합 끝남, integ/m2는 agent-adf1305a61821e485로 M2 병합 때까지 유지).

## 다음 세션 첫 행동
1. `git status`(체크아웃 main, `.claude/` 미추적은 worktree 폴더)·이 파일·`docs/approvals.md` 끝 3행(A-084~A-086)·`docs/worklog.md` 마지막 항목(43) 확인.
2. **10-12(월) 첫 무인 자동 운영 결과 확인**(PAPER-0 4일차, V-212 실측). 사용자에게서 텔레그램 결과를 받거나 "로그 봐 줘" 지시가 오면 읽기 전용으로: `var\paper\logs\autopilot-20261012.log`·`run-console-20261012.log`·`ops-20261012.jsonl`, `var\paper\review\`, 작업 스케줄러 "마지막 실행 결과"(종료 코드 0/1/2/3). 볼 것: 06:40 ClockSync·06:45 시작 여부, 시작 점검 시계 오차(시간 서버 변경 효과, 3일차 최대 0.981초), K8 자동 재개, 07:53 K5 자동 해제, 09:30 매수·14:30 매도, CLOSE MATCH, 15:45 종료·집계·백업·요약, "확인 필요" 줄. 이번 2e2f713·4e76347·9a494f1 보강의 **첫 실제 실행**이다.
   - **시계 관찰(사용자 "오늘 지켜보자", 2026-10-11)**: 10-11 21:33 실측 w32tm 출처 time.kriss.re.kr(설정 정상), 마지막 동기화 20:56, 오차 약 −0.59초(37분 만에). 과거 clock_check 최대: 10-07 0.981초(0.8초 초과 4회), 10-08 0.269초. 10-12 run의 clock_check(10분마다, 1초 초과 → K6) **장중 최대 오차**를 읽기 전용으로 집계 → 0.5초를 자주 넘으면 Windows 동기화 주기 단축(관리자 명령, 사용자 실행) 또는 장중 ClockSync 추가 제안. 실전·M2 테스트넷(-1021 민감) 전 필수 항목. 같은 날 전략실 검증(작업 프로세스 8개)이 밤새 돌았을 수 있음 — 10-11 21:3x CPU 약 30%, 12코어·64GB라 영향 낮다고 판단, 결과 해석 때 함께 볼 것.
   - 06:50까지 "run 시작"이 없었다면: PC 로그인 상태 여부(예약 작업 Interactive), 루트 `.env` 모드 줄, `autopilot-fatal.log`, `alerts-fallback-autopilot.log` 순서로 확인.
3. 결과를 07-paper-review §12B 형식(4일차)으로 기록. 무인 운영일에는 엔진 "일일 보고"(16:00)가 생성되지 않는다(run 15:45 종료) — 텔레그램 요약은 정상. 설계 공백(낮음).
4. 10-12 성공 뒤: **M2 병합(A-086 조건부 승인 — 성공 확인되면 저녁에 병합, 사용자 재지시 불필요)**(위 표, M1 변화 6가지를 사용자에게 먼저 알림), PAPER-0 종료 판정 준비(권장 3~5거래일, 무인일 포함). M2 결정 필요 항목 질문.
5. **CP-032 후속 (A-085)**:
   - 실측 도구 실행(**사용자** — 에이전트 실행은 권한 거부됨, 저녁·주말, PAPER run 꺼진 뒤, main에 있음): `cd C:\AI\trading_system\trading_system` → `$env:TRADE_MODE='PAPER'` → `.venv\Scripts\python scripts\kiwoom-probe.py --mode PAPER --codes 005930,000660,069500`(약 1~2분, 결과 `var\paper\probe\<시각>-probe.md`, 설명서 §11). 종목 000660·069500은 에이전트 제안. 결과는 kiwoom-kr에 "② 모의 실측"으로 기록 → K-U30·K-U31·K-U32·K-U34 판정 → **단계 2 승인 질문**(별도).
   - Q-3(모의 지정가 ①② 실측)·Q-4·Q-5는 미결. 지정가 PAPER 사용은 Q-3 뒤.
   - 사용자 오해 주의: 현재 PAPER 지원 = 일봉·정해진 시각 판정·시장가·고정 종목 목록. T-027은 오프라인 구현이라 지정가 전략을 PAPER에 쓸 수 없다.
6. 사용자는 터미널 명령에 익숙하지 않다 — 실행 안내는 **복사해 붙일 전체 명령**, 용어는 풀어서. "실전"은 LIVE에만 쓴다(모의 서버 실행은 "모의 서버 실행"). 평일 07:15 출발·장중 부재·텔레그램만 가능.

## 전략실 (2026-10-09 생성, A-085) — `C:\AI\strategy-lab`, 별도 git 저장소(커밋 없음 — 사용자 지시 시)
- 만든 것: `CLAUDE.md`(금지: 브로커 연결·주문·비밀정보·운영 저장소 쓰기·평일 06:40~16:00 무거운 실행), `README.md`(복사용 명령), `tools\sync-data.ps1`(운영 일봉 30종목 복사, **2026-01-01 이후 행 잘라 냄**, 휴장일 JSON 복사), `tools\validate.ps1`(운영 .venv + PYTHONPATH로 운영 `scripts\validate-strategy.py` 실행, 결과는 전략실 `results\`, 평일 낮 거부·휴장일 `-Holiday`), 견본 `candidates\example_sma\`(strategy.py·params-v1.toml·strategy.md·notes.md). .ps1은 UTF-8 BOM(PS 5.1 한글).
- **확인된 사실(2026-10-09 실제 실행)**: 절대 경로 params·out-root·전략실 모듈 import 동작, 운영 저장소 변경 0. 견본 91회 실행 약 86분(시험과 동시), 판정 미충족(견본이라 무의미).
- 아래는 생성 전 설계 기록(참고).
- 목적: 사용자가 실전용 전략을 만들 단계(PAPER-0 무인 검증 뒤 PAPER-1 필요). 연구(많이 만들고 버림)와 운영(승인된 것만)을 분리.
- 위치·구조(제안): 별도 git 저장소 `C:\AI\strategy-lab\` — `CLAUDE.md`(가벼운 규칙: 브로커 연결·주문·비밀정보·운영 저장소 쓰기 금지), `ideas\`(아이디어·다른 AI와 만든 초안), `research\`(노트북·pandas 자유), `data\`(일봉 CSV 사본, git 제외 — 원본 trading_system `var\backtest\data\kiwoom-ka10081-adj-20261007`), `candidates\<전략ID>\`(strategy.md 양식, `<id>.py` Strategy 규격, params.toml), `results\`.
- 미리 돌려 보기(운영 엔진 재사용, 확인된 사실): `scripts\validate-strategy.py`는 `--strategy 모듈:클래스`(PYTHONPATH로 전략실 경로 추가), `--params`(저장소 기준 경로지만 절대 경로도 pathlib상 가능), `--data` 임의 폴더, `--out-root` 지정 가능. 예: `$env:PYTHONPATH='C:\AI\trading_system\trading_system\src;C:\AI\strategy-lab'` 후 운영 .venv python으로 실행, 결과는 전략실 `results\`. **생성 때 실제로 1회 돌려 확인하고 전략실 스크립트로 고정**할 것(미검증: 절대 경로 params, --out-root 절대 경로, 장부 경로). 2026 이후 데이터는 자동 거부. 장중 실행은 피한다(같은 PC 자원).
- 공식 판정·적용(운영 저장소): 후보 3파일을 `src\trading_system\strategy\`·`config\strategies\`·`docs\strategies\<ID>\`로 복사(브랜치) → 구조 시험(전략은 표준 라이브러리만, 네트워크·파일 금지) → validate-strategy 공식 판정표(CP-030 C-1~C-8) → `strategy.py register` → BACKTEST_PASSED(A-ID) → 사용자 승인 PAPER → `config\app.paper.toml` `[[strategies]]` 추가 → PAPER-1.
- 제약: 계좌 한도(L1~L7: 보유 3·일 5건·1회 100만 원)는 전략 공유. 지원 능력 밖이면 CP-032 해당 단계가 먼저. 검증용 paper0-verify는 PAPER-0 종료 뒤 교체.

## 결정 필요 (급하지 않음, 사용자)
1. 문서 승인 묶음(CP-025 먼저 — 코드는 main에 있음; 00~05·업무정의서 draft, draft-bundle-review 요약). DP-31·DP-40 재검토(decisions-pending "재검토 대기").
2. M2 다음 작업(T-M2-08~)과 테스트넷 연결(T-M2-14) 승인. **CP-032(기능 확장, draft)** Q-1 지정가 단계 승인 / Q-2 모의 서버 조회·구독 실측(주문 없음) / Q-3 모의 지정가·정정 실측(PAPER 주문, 호가 격자 500·250원 확인 포함) / Q-4 단타 프로필 값 / Q-5 다종목·동적 유니버스 CP 시점.
3. 전략: 사용자 2026-10-09 "자동 테스트만 하면 실전에 쓸 전략을 만들어야" — 전략실에서 시작 예정(위). 보존: trend-sma 초안·30종목 백테스트(docs/strategies/trend-sma/backtest-v1-multi.md), 스크리닝 아이디어(전 종목 → 거래 가능 필터 → 추세 → 상대강도, 보유 20). 재개 때 T-026 공통 기준(CP-030)으로 판정.
4. 시스템 과제 후보: 동적 유니버스, 다종목 동시 운영, 정정(CAP-ORD-03), 지정가·호가 단위(X-U1 공식 확인 착수 A-084), 게이트 L1(상한가 기준)과 전략 위험 계산 정합, M2 [제안] 12건, CP-027 호가 단위 문서·코드 차이, V-180.
5. 사용 설명서 v0.2 [확인 필요] 항목: w32tm 한글 출력 문구, 저녁에 기록한 명령이 다음 날 아침 처리되는지, 작업 스케줄러 11시간 상한 때 run 동작.
6. 호가 격자: KRX 표(200k~500k = 500원) 확인, 모의 체결가 250원 격자는 KRX 중간가주문(2025-03-04)으로 설명[부분 확인] — 지정가 PAPER 사용 전 모의 실측(CP-032 Q-3).

## 진행 중
- A-084 후속 전부 main 병합 완료. 남은 낮음(기록): ~~손상 DAILY_BASE 복구 절차 문서 없음~~(설명서 부록 A로 해소, 2026-10-09 — 날짜·시각 모두 읽을 수 없는 손상 행은 매일 매수가 막히는 문제 남음, CP 필요할 수 있음), 무인 운영일 엔진 일일 보고 미생성, confirm-unknown 목록의 immutable 읽기(run 시작 직후 오래된 내용 가능 — 읽기 오류면 거부), 429 시 requests 시간 초과가 전체 한도가 아님(30초는 최선 노력). A-084의 "429 상한 60초"는 "메시지당 총 30초·종료 전 재전송 총 60초"로 대체(더 보수적, 에이전트 판단).
- A-084 2-2 CAP-ORD-02 "미지원(X-U1 전까지)"는 docs/changes/CP-024-capabilities-draft.md에 선반영 — **docs/capabilities.md 실제 반영은 PAPER run이 꺼진 저녁 + 전체 회귀 + 사용자 지시**. 2-3 텔레그램 금액 제외·비율은 01 §2.10·03 §6.6 반영, 코드는 ops-hardening에서.
- CP-029 적용 완료 기준 중 남은 것: 사용자 예약 작업 등록 + 10-12 첫 무인 운영일 결과 확인(05 T-025).

## 현재 상태 요약
- **M1**: 6단계, PAPER-0 3거래일 완료(10-06·07·08), 왕복 2건, 결과 불명·불일치·중복 0. 전략 paper0-verify **v2** PAPER(v1 STOPPED REPLACE, A-067). 보유 0. PAPER 수수료 0.35%(A-075, 키움은 10원 미만 절사). 07-paper-review v0.4(3일차 DB·로그 실측). 시계 최대 오차 0.981초(허용 1초) — 시간 서버 변경 필요.
- **M2(바이낸스)**: 설계 CP-024 v0.5(A-066), 구현 시작 A-071. T-M2-01·02·03·07·10 완료, `integ/backtest-m2` 통합 1758 passed(미병합). 테스트넷 연결·실전 주문 코드·LIVE 승인 없음.
- **문서**: CP-026~CP-030 승인, CP-031 A-084 위임 승인, CP-025 미승인. 00~05 draft에 `[CP-025]`~`[CP-031]` 표시 반영 완료(main).

## 로드맵 (승인 단위)
| 순서 | 작업 | 선행·승인 |
|---|---|---|
| 1 | 10-12 무인 자동 운영 시작 → PAPER-0 계속(3~5거래일 권장 중 3일 경과) | 사용자 등록 4단계 |
| 2 | M2 병합(integ/m2-2026-10-09 9df2fd2, 2293 passed — 재통합·T-M2-04~13 완료·T-M2-09 대부분) | 10-12 성공 뒤, 사용자 지시 |
| 2a | CP-032 단계 1 지정가: 오프라인 구현·capabilities 반영 완료(main 3ee9f97) → 실측 도구 사용자 실행 → Q-3 모의 지정가 실측 → PAPER 사용 | Q-1 승인 A-085, Q-3 미승인 |
| 3 | 문서 승인 묶음(CP-025 → 00~05 draft) · DP-31·40 재검토 | 사용자 |
| 4 | PAPER-0 종료 판정(PV 항목별) → T-018 실제 전략 결정(T-026 판정) → T-019 PAPER-1 | 사용자 |
| 5 | M2 남은 것: V-157 미실행 4개 → 테스트넷 연결·엔진 조립 T-M2-14(별도 승인) → M2 PAPER-0 | 사용자 |
| 6 | LIVE 준비(시장별, 04 §5.1 C-01~C-14) | 별도 승인 |

## 도구 (사람 실행, 먼저 `cd C:\AI\trading_system\trading_system`)
- 자동 운영 시작(수동 1회) `.venv\Scripts\python scripts\autopilot.py --mode PAPER` / 오늘 run 끝내기 `.venv\Scripts\python scripts\ops-stop.py --mode PAPER`
- 예약 작업 등록·해제(관리자 PowerShell) `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\schedule-register.ps1` / `...\schedule-unregister.ps1`
- 점검 `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` (run 실행 중에는 거부) / 수동 실행 `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run`
- `.venv\Scripts\python scripts\<도구>.py --mode PAPER`: `status`, `resume K8|K4|K1`, `kill`, `reconcile-resolve`, `confirm-unknown`(결과 불명 확정: `--list` / `<의도ID> --not-accepted` / `<의도ID> --accepted --order-no <번호>`, CP-031 K3 해소), `paper-review`, `telegram-test`, `backup`, `restore-test`, `strategy show|register|transition`, `watchdog`, `migrate`, `archive-logs`
- 데이터·백테스트: `scripts\fetch-daily.py`(키움 일봉 조회, run 종료 뒤에만 A-073 E), `scripts\validate-strategy.py`(CP-030 검증 실행기, main. 2026-01-01 이후 데이터는 `--final-check`로 버전당 1회만; 백테스트는 실행마다 trades.csv·equity.csv·summary.json), `python -m trading_system.app backtest ...`
- 에이전트 대화 지시 실행 범위(A-079): 시작·정지·재개 명령 기록·status·paper-review·backup만. LIVE·실전 주문·불일치 해소 방법 선택·지시 없는 재개 금지.
- 권한 `.claude/settings.local.json`: push·reset·rebase·clean·pip install·비밀 파일 접근·`rm -rf` 차단. **명령문에 비밀 파일 이름이 들어가면 거부**(스크립트 파일로 우회하지 않음).

## 남은 위험·주의
- 무인 운영: 자동 운영 프로세스 자체가 죽으면 그날 종료·요약 없음(저녁 요약 부재로 확인), 설정 오류로 텔레그램 못 쓰면 알림 없음(06:50 "run 시작" 부재로 확인), 작업 스케줄러 11시간 상한 시 자식 run 동반 종료 여부 미확인, Interactive 작업이라 로그아웃·재부팅 뒤 미실행.
- 07:53 K5는 3일 연속 재현(모의 서버) — CP-028 P4로 07:58 해제 확인. 텔레그램 429 상한 이내 지연은 K11 없음, 내용 거부 긴급 알림은 로컬만.
- K-U14 행 단위 미확인(부분 체결 여러 번인 날) → K4 보수적. 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거. CP-031 적용 전까지 M1 결과 불명 5회 미발견은 자동 미접수 종결(현 main).
- **문서 편집 주의**: heredoc·Python 문자열에 `scripts\resume.py` 같은 경로를 쓰면 `\r`이 줄바꿈으로 깨진다. 경로가 든 문서 수정은 Edit/Write 도구로, 수정 뒤 `grep -nP "[\x00-\x08\x0b\x0c\x0e-\x1f]" docs -r` 검사. `docs/capabilities.md`는 실행 코드가 읽으므로 PAPER 중 직접 수정 금지.
- 정리 필요(사용자): `C:\Users\user\AppData\Local\Temp\v91-p8kqynon` 임시 폴더.

## LIVE 전 필수
- 기준: **04 §5.1 C-01~C-14 체크리스트 + 07-paper-review §13 현재 상태표**(시장별 판정). 아래는 거기 없는 추가 항목만.
- 달력 KRX 원문 대조 + 사용자 검증 데이터(A-037), 2026-11-19 수능일 KRX 공지 확인.
- **시계**: 모의 운영 기간 장중 최대 오차 기준(제안 0.5초 이하) 충족 — Windows 동기화 주기 조정 등(2026-10-11 관찰 시작). M2는 바이낸스 -1021 때문에 더 엄격히.
- 실전 API 호출 제한·실전 수수료 요율(A-075: LIVE 준비 때 별도 설정)·호가 단위(X-U1) 공식 원문.
- 자동매매 전용 실계좌(A-051), 백업 외부 저장소(A-039), 노출된 모의 키 재발급, 밤샘 실행 검증(PV-11), 작업 스케줄러 상한 동작 실측.
- 자동 운영은 PAPER 전용 — LIVE 자동화는 별도 CP·승인 없이는 없음. 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자).

## 작업 방식
- 테스트 통과 → 로컬 커밋 → 이 파일 갱신, worklog 항목 15줄 이내. 문서 커밋 메시지 `docs: ...`.
- 수정 뒤 독립 재검토 1회 이상. 재검토 지적은 코드로 사실 확인 후 반영(틀린 지적 있음).
- 병렬 에이전트는 읽기 전용 또는 파일 소유 분리, 커밋은 리드만. 다른 에이전트 소유 파일(broker-specs 등)은 건드리지 않는다.
- 실행하지 않은 시험을 실행했다고 쓰지 않는다. 3일차 수치처럼 보고 기반이면 [보고]·미확인 표시.
