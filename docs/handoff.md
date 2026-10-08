# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: 2026-10-08 저녁. **PAPER-0 3일차(10-08) "보는 자동 운영" 성공** — 06:44 K8 자동 재개(7초), 07:53 K5 → 07:58 자동 해제(CP-028 P4), 09:30 매수 267,500 / 14:30 매도 266,250, 마감 대조 MATCH, 15:45 자동 종료·paper-review·백업·요약 정상. 결함 2건(요약 기준 금액 표시, 15:09 하트비트 거짓 경보) 수정 013f2bd 병합. 실제 비용 수수료 1,860 + 세금 532, 손익 −3,642(시스템 −3,652, 10원 올림 차). 마지막 승인 기록 **A-084**. 운영 카드: https://claude.ai/artifact/XXZrAhidXPAFo1hUDRgVdw
- **체크아웃 `main`** HEAD **4e76347**(2026-10-09 새벽 2차 병합 `integ/final`: 백테스트 결과 파일·검증 실행기, 결과 불명 확정 도구 confirm-unknown, 운영 낮음 항목, test_r4 간헐 실패 보강, 문서 — 사용 설명서 v0.2·CP-032 draft·키움 확장 사양 조사·호가 단위 재확인·07 v0.4). 통합 브랜치 전체 **1449 passed**(942.80s). 1차 병합(9a494f1, 1335 passed): 운영 묶음 ops-hardening·code-health·unknown-k3(CP-031). push 없음. 10-09 한글날 휴장. **다음 거래일 10-12(월)부터 무인 자동 운영 예정** — 사용자 준비(아래)가 끝나야 한다. **PC는 10-09 새벽 사용자 지시로 종료됨** — 귀가 후 켜고 로그인해야 한다.
- 실전 주문 코드·LIVE 승인 없음. 게이트 변경 없음(00~05·업무정의서 draft 묶음 승인 전, 검토 요약 docs/changes/draft-bundle-review-2026-10-07.md).

## 브랜치·worktree (2026-10-09 새벽)
**미병합 — M2만 남음 (에이전트 판단: 10-12 첫 무인 운영 성공 뒤 병합)**
| 브랜치 (worktree) | HEAD | 전체 시험 | 상태 |
|---|---|---|---|
| integ/backtest-m2 (integ2) | cb698ed | **1758 passed** | main d59095f + 백테스트 + M2(T-M2-01·02·03·07·10) 통합. cb698ed = M1 daily_base가 market≠M1 행을 건너뜀(시장 충돌 수정, 시험 3건). 충돌 0건 |
- M2 병합 때: main(4e76347) 위에 새 통합 브랜치 → `integ/backtest-m2`의 M2 병합분(feat/T-M2-02-03 → feat/T-M2-07-cash-risk → feat/T-M2-10-adapter + cb698ed) 다시 합치기 → 전체 회귀 → main. 남은 위험: paper_review DAILY_BASE 목록이 시장 구분 없음(보고용), spot_cash._saved_day가 같은 별칭의 깨진 M1 행에 예외(별칭 분리로 현실성 낮음), 바이낸스 어댑터는 합성 응답 시험만(B-U4·B-U10).

**병합 완료 (2026-10-09)**
- 1차(지시 "운영 묶음과 문서 main 병합"): ops-hardening(7e71480·d71ebc1·242476e), code-health(6bac802·f6d5090), unknown-k3(0f6aef6·2109f81), docs/cp-030-validation-criteria, docs/user-manual → 9a494f1, main 1335 passed.
- 2차(지시 "남은 개발 전부 시행"): integ/final = fc2790f(test_r4 대기 보강) + integ/backtest(T-009 295e74d·T-026 03132f7, 충돌 0) + fix/confirm-unknown-cli(15b5ceb·f836f12, 재검토 반영) + docs/manual-v02-cp032 + docs/paper-review-v04(kiwoom-kr.md 충돌 1건 — §7e 다음에 확장 사양 조사 절, 둘 다 보존) + fix/ops-low-items(c7e16da·b2b535e, 재검토 반영) → 4e76347.
- 정리 대상 worktree(병합 끝남, 사용자가 `git worktree remove ..\<이름>` 실행 — 에이전트 권한 거부): cp028·autopilot·t018-multi·day3-fixes·ops-hardening·code-health·unknown-k3·integ·docs-cp030·docs-manual·bt-export·t026·confirm-cli·ops-low·docs2·docs3·final·m2-t07(M2 병합 뒤)·m2-binance·m2-adapter·m2-next(M2 병합 뒤). integ2는 M2 병합 때까지 유지.

## 다음 세션 첫 행동
1. `git status`(main 4e76347)·이 파일·`docs/approvals.md` 끝 3행(A-082~A-084)·`docs/worklog.md` 마지막 항목(41) 확인.
2. 사용자에게 **10-12 무인 전환 준비**를 안내 — 사용 설명서 **docs/user-manual.md v0.2 §2**(시간 서버 time.kriss.re.kr 화면·명령, `.env` TRADE_MODE 확인, 예약 등록, 실행 계정 확인, 업데이트 사용 시간·절전). 복사해 붙일 전체 명령으로.
3. 10-12부터 자동 운영일 관찰(K8 자동 재개, 07:53 K5, 15:45 종료 절차, 종료 코드, V-212)을 07-paper-review §12B 형식으로 날마다 기록. 무인 운영일에는 엔진 "일일 보고"(16:00)가 생성되지 않는다(run 15:45 종료) — 텔레그램 요약은 정상. 설계 공백으로 기록(낮음).
4. 10-12 성공 뒤: M2 병합(위 절차), CP-032 승인 질문(Q-1~Q-5), PAPER-0 종료 판정 준비.
5. 사용자는 터미널 명령에 익숙하지 않다 — 실행 안내는 **복사해 붙일 전체 명령**, 용어는 풀어서. "실전"은 LIVE에만 쓴다(모의 서버 실행은 "모의 서버 실행"). 평일 07:15 출발·장중 부재·텔레그램만 가능.

## 결정 필요 (급하지 않음, 사용자)
1. 문서 승인 묶음(CP-025 먼저 — 코드는 main에 있음; 00~05·업무정의서 draft, draft-bundle-review 요약). DP-31·DP-40 재검토(decisions-pending "재검토 대기").
2. M2 다음 작업(T-M2-08~)과 테스트넷 연결(T-M2-14) 승인. **CP-032(기능 확장, draft)** Q-1 지정가 단계 승인 / Q-2 모의 서버 조회·구독 실측(주문 없음) / Q-3 모의 지정가·정정 실측(PAPER 주문, 호가 격자 500·250원 확인 포함) / Q-4 단타 프로필 값 / Q-5 다종목·동적 유니버스 CP 시점.
3. 전략은 뒤로(사용자 2026-10-08: 시스템 검증·사용법 우선). 보존: trend-sma 초안·30종목 백테스트(docs/strategies/trend-sma/backtest-v1-multi.md), 스크리닝 아이디어(전 종목 → 거래 가능 필터 → 추세 → 상대강도, 보유 20). 재개 때 T-026 공통 기준(CP-030)으로 판정.
4. 시스템 과제 후보: 동적 유니버스, 다종목 동시 운영, 정정(CAP-ORD-03), 지정가·호가 단위(X-U1 공식 확인 착수 A-084), 게이트 L1(상한가 기준)과 전략 위험 계산 정합, M2 [제안] 12건, CP-027 호가 단위 문서·코드 차이, V-180.
5. 사용 설명서 v0.2 [확인 필요] 항목: w32tm 한글 출력 문구, 저녁에 기록한 명령이 다음 날 아침 처리되는지, 작업 스케줄러 11시간 상한 때 run 동작.
6. 호가 격자: KRX 표(200k~500k = 500원) 확인, 모의 체결가 250원 격자는 KRX 중간가주문(2025-03-04)으로 설명[부분 확인] — 지정가 PAPER 사용 전 모의 실측(CP-032 Q-3).

## 진행 중
- A-084 후속 전부 main 병합 완료. 남은 낮음(기록): 손상 DAILY_BASE 복구 절차 문서 없음, 무인 운영일 엔진 일일 보고 미생성, confirm-unknown 목록의 immutable 읽기(run 시작 직후 오래된 내용 가능 — 읽기 오류면 거부), 429 시 requests 시간 초과가 전체 한도가 아님(30초는 최선 노력). A-084의 "429 상한 60초"는 "메시지당 총 30초·종료 전 재전송 총 60초"로 대체(더 보수적, 에이전트 판단).
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
| 2 | M2 병합(integ/backtest-m2 기반 재통합) | 10-12 성공 뒤, 전체 회귀 |
| 2a | CP-032 단계 1 지정가(오프라인) → 모의 실측 → PAPER 사용 | CP-032 Q-1·Q-3 승인 |
| 3 | 문서 승인 묶음(CP-025 → 00~05 draft) · DP-31·40 재검토 | 사용자 |
| 4 | PAPER-0 종료 판정(PV 항목별) → T-018 실제 전략 결정(T-026 판정) → T-019 PAPER-1 | 사용자 |
| 5 | M2 T-M2-08~ 구현 → 테스트넷 연결(별도 승인) → M2 PAPER-0 | 사용자 |
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
- 실전 API 호출 제한·실전 수수료 요율(A-075: LIVE 준비 때 별도 설정)·호가 단위(X-U1) 공식 원문.
- 자동매매 전용 실계좌(A-051), 백업 외부 저장소(A-039), 노출된 모의 키 재발급, 밤샘 실행 검증(PV-11), 작업 스케줄러 상한 동작 실측.
- 자동 운영은 PAPER 전용 — LIVE 자동화는 별도 CP·승인 없이는 없음. 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자).

## 작업 방식
- 테스트 통과 → 로컬 커밋 → 이 파일 갱신, worklog 항목 15줄 이내. 문서 커밋 메시지 `docs: ...`.
- 수정 뒤 독립 재검토 1회 이상. 재검토 지적은 코드로 사실 확인 후 반영(틀린 지적 있음).
- 병렬 에이전트는 읽기 전용 또는 파일 소유 분리, 커밋은 리드만. 다른 에이전트 소유 파일(broker-specs 등)은 건드리지 않는다.
- 실행하지 않은 시험을 실행했다고 쓰지 않는다. 3일차 수치처럼 보고 기반이면 [보고]·미확인 표시.
