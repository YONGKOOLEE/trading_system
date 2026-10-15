# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: **2026-10-14 밤(세션 46 끝)**. 마지막 승인 기록 **A-103**(다음 번호 A-104). 체크아웃 main. 사용자가 밤에 push함(origin 갱신) — 이후 로컬 커밋 몇 개는 push 대기(`git status -sb`로 확인, push는 사용자 실행 — 에이전트 권한 차단).
- **계획 문서 재승인 A-102**: 업무정의서 v0.7·00 v0.5·01 v0.9·02 v0.8·03 v1.0·04 v0.9·05 v0.6 approved(CP-024~034 반영, CP-032 단계 3~5 제외). **승인 제외(미결)**: 01 K9 교체 행 vs 04 V-180 충돌. 이전 승인본 archive superseded. 검토 요약 `docs/changes/draft-bundle-review-2026-10-14.md`(보류 H-1~H-9, §6 미결 목록).
- **PAPER-0 조건부 통과(A-097)**: 10-06·07·08(수동·반자동)·10-12·13(**무인**) 5거래일, 왕복 4건, 결과 불명·중복·불일치 0. 10-13: 시작 점검 시계 0.06초·장중 최대 0.353초, 매수 237,500·매도 232,500, 15:45 종료 코드 0. 07-paper-review §12D·§12E. **매일 06:45 무인 자동 운영 유지**(paper0-verify v2, 예약 작업 `TradingSystem-PAPER-Autopilot`·`ClockSync`).
- **main 최신 시험**: 통합 2763 passed/1 skipped(T-028·T-M2-14 병합 시점), 이후 PV-12 도구 병합(73 passed, 에이전트 전체 2836 passed). 실제 `config/*.paper.toml`로 M1 시작 일치·DB 메타·공통 설정 오프라인 검사 통과.
- **10-14(수) 자동 운영 미실행**: 05:44·05:48 Windows Update 계획 재시작 → 08:52 로그온 전이라 Interactive 예약 작업 건너뜀(CP-029 R-4 발생, 07 §12F). 대책 **A-098**: 업데이트 사용 시간 05:00~23:00(확인), 업데이트 후 자동 로그인(사용자 보고). D-158 유지. 내부 상태 이상 없음.
- 실전 주문 코드·LIVE 승인 **없음**.

## 지금 main에 들어 있는 것 (이번 주 병합)
| 항목 | 상태 |
|---|---|
| M2(바이낸스) 오프라인 구현 T-M2-01~14 | 병합(c2ed1e1, 722d0c2). 테스트넷은 **조회만**·주문 계열 19경로 전송 전 차단. 유니버스 BTCUSDT(A-096). **테스트넷 시작 점검 통과 2026-10-14 23:06**(서버 시각 57ms, BTCUSDT 규칙·rateLimits — binance-spot §12, PV-M2-03). 사용자 키·접속 스위치 입력 완료 |
| CP-032 단계 1 지정가(T-027) | 오프라인 구현·능력 표 "검증 중". PAPER 사용 금지(Q-3 미승인) |
| CP-032 단계 2 분봉(T-028) | 수집·저장·정규장 필터·`fetch-minute.py` 병합. 판정 연결(T-029)은 우선순위 낮음, 분봉 전략 검증 기준 보류(A-095) |
| CP-034(L1 여유 3%·정책 프로필·전략별 슬롯·U-B 목록·배분 규칙) | 승인 A-092·A-093·A-094, 구현 병합(79933ba). paper0 값 불변. 프로필 파일 `config/limits.research.toml`·`limits.paper1.toml` |
| PV-12 호출 제한 도구 | 병합(05044df), **실행 완료** — 1.0초 간격 2번째에 1700, 1초 대기로 회복(K-U10). 운영 1.1초 유지 |
| 실측 도구 3종 | `kiwoom-probe.py`(분봉·틱·WS 0B/0D, 실행 완료), `investor-probe.py`(수급 ka10060, 실행 완료), `rate-probe.py`(실행 완료) |
| 수집 | 코스피200 201종목 일봉+KOSPI 지수(`var\backtest\data\kiwoom-kospi200-adj-20261009`), 투자자별 ka10060 201종목(`kiwoom-investor-20261008`), 005930 깊은 조회(`kiwoom-investor-probe`) |

## 다음 세션 첫 행동
1. `git status`·이 파일·`docs/approvals.md` 끝 7행(A-099~A-105)·`docs/worklog.md` 마지막 항목(46) 확인. 실행 모드 확인은 `status.py --mode PAPER`(읽기).
2. **10-16(금)**: 06:45 자동 운영은 K4(사람 확인 정지)라 매매 없음(설계대로). 첫 실행 코드: main 12c4d36(CP-033 저녁 수집 꺼짐·CP-035 시작 검사·T-044) — 06:50 텔레그램 "시작 점검 통과"와 K4 안내 확인. A-098 대책 2일째. 10-15 결과는 07 §12G.
2-0. **10-15 직접 매수 감지 시험 결과·정리 대기**: 사용자가 **005930**(전략 보유 종목)을 1주 직접 매수 → 09:51:14 주기 대조가 QTY_MISMATCH(내부 1·브로커 2)·UNKNOWN_BROKER_ORDER(BUY 1) 감지 → **K4**·긴급 알림 2건(감지 성공). 사용자가 당일 1주 되팔지 못함 → **2주 보유(v2 소유 1 + 수동 1)**, 14:30 전략 매도는 K4로 막힘. EXTERNAL은 전략 소유 종목이라 거부되므로(ops/reconcile.py) 정리 = 사용자가 장중에 **1주만** 앱 매도 → 저녁 PC에서 항목마다 `reconcile-resolve <ID> BROKER_ACTION`(매도 주문 항목 포함) → `resume.py --mode PAPER K4` 기록 → 다음 시작에 재대조 일치 시 해제. 10-16(금) 06:45 자동 운영은 K4(사람 확인 정지)라 자동 재개 안 함 → 그날 매매 없음. PV-11 시작 조건(§3)은 "보유 0" 대신 v2 1주 보유 허용으로 조정 필요(주말 K8 유지라 주문 없음). 07 §12G에 기록함. PV-11 절차 §3 조정함(566fa0c).
2-2. **CP-036 M2 실행 경로 병합 대기 (A-106)**: worktree `.claude/worktrees/agent-a1965f48de2df7781` 브랜치 `feat/T-049-m2-ops` = 6688267(T-045) → 4a86ea5(T-046) → 7950b25(T-047) → 0a799fb(T-048) → a52f7d7(T-049) → **406ab6a(재검토 수정)**, 전체 3125 passed/1 skipped. 재검토 3회(T-045·046 M1 불변 확인, T-047~049 주문 안전 OK). T-047은 부하로 soak 시험 1건 실패한 채 커밋됐던 규칙 위반 — 단독 재실행 통과 기록. capabilities.md M2 열·CAP-INS-05 행 변경(M1 칸 불변) — **병합 전 사용자 확인**, M1 꺼진 시간에. DP-51~DP-69 결정 대기. 다음: T-050 연결 승인(Q-5)·M2 텔레그램 봇 생성 안내. 별도 worktree `agent-a314ab2373f731e73`(m2-verify 초안, 옛 main 기준)은 병합 전 갱신 필요. 01:0x 구현 에이전트가 `taskkill /IM python.exe`(창 제목 필터) 실행 — 종료된 프로세스 없음 확인(01:32), 메모리 규칙 추가.
2-1. **병합 완료(10-15 저녁)**: CP-033(T-035~T-037, main 62ba6f0) → main 전체 2938 passed/1 skipped. CP-035+T-044(00dcabb·1b82ae5·850b903, main 12c4d36, 충돌 decisions-pending만 수동 해소) → main 전체 **2951 passed/1 skipped**. 저녁 수집 스위치 `config/collect.paper.toml` enabled = false — 켜기는 사용자(A-100). 결정 대기 DP-46·DP-47(05a 결측 시 청산 거부 — 05a 전략 배포 전 차단)·DP-48(첫 실행 확인)·DP-49(CP-035 Q-2 시작 거부 = A-105로 확정)·DP-50(판정 기록 선저장 → 사이 장애 시 신호 유실). 05-plan(approved A-102) T-035~T-037·T-043·T-044 진행 상태는 다음 draft에 반영(문서 개정 시).
3. **텔레그램 `[M1]` 접두어**: 10-13부터 붙는다. 사용자에게 화면 확인을 한 번 요청(로그에는 안 남음).
4. **M2**: 테스트넷 시작 점검 **통과(2026-10-14, PV-M2-03 기록)**. 키는 m2-binance 폴더의 자격정보 파일에 사용자가 입력 완료(TRADE_MODE 줄 첫 시도 누락 → 수정), 접속 스위치 켜짐(m2-binance 작업 트리의 설정 파일 수정 — 커밋하지 않음). 다음은 T-M2-15 M2 PAPER-0(테스트넷 모의 주문) — **검증 전략(종목·시각·수량) 승인 + 별도 승인** 필요. **검증 전략 초안 m2-verify v1 작성(미승인·미병합)**: worktree `.claude/worktrees/agent-a314ab2373f731e73` 브랜치 `feat/T-M2-15-verify-strategy` 36e4b2d(전체 2897 passed) — 권장 KST 10:00 매수·13:00 매도·10 USDT. **M2 실행 경로(엔진 조립) 없음**(app/main.py `run --market M2` 거부) → B-2 등 CP 필요(사용자에게 CP 작성 여부 질문 중), B-1 테스트넷 지급 BTC 1개가 수동 보유로 잡혀 진입 G6 거부(처리 방법 사용자 결정), B-6 시장가 매도 수량 내림 규칙 불일치. 이전 초안 docs/strategies/m2-paper0-verify(10-05)는 정리 대상.
5. **PV-11 주말 장시간 실행 — 승인 A-099**, 절차 `docs/strategies/paper0-verify/pv11-weekend-run.md` v0.2. 금 10-16 저녁 사용자 시작(자동 운영 요약 수신 뒤 status → preflight → 수동 run, **K8 풀지 않음**), **일요일 21:00~22:30 정지**(`ops-stop.py` → backup → paper-review → status: 정지는 K8·K9뿐이어야 월요일 자동 재개). Windows Update 1주 일시 중지(사용자). 토큰 발급 감사 기록 `TOKEN_ISSUED`(INITIAL·REFRESH·REISSUE, REFRESH는 텔레그램 INFO "토큰 갱신") main 반영 — **10-15 아침 run에서 INITIAL 행 생기는지 확인**(첫 실제 실행). 끝나면 사용자 "주말 실행 끝" → 절차 §8대로 07 §12G 기록.
6. 응답 방식: 사용자는 터미널에 익숙하지 않다 — **복사해 붙일 전체 명령**, 쉼표 목록은 큰따옴표(`--codes "005930,000660"`), 용어 풀어서. "실전"은 LIVE에만. 평일 07:15 출발·장중 부재·텔레그램만 가능(10-14 수동 매수는 휴대폰 앱으로 가능).

## 사용자 결정 대기 (급하지 않음)
1. **실제 매매 전략** — 전략실(별도 세션)에서 연구 중. 전략이 정해져야 PAPER-1(최소 20거래일) 시작. 지금 가장 큰 병목.
2. **CP-033 승인 A-100**(Q-6 (a) 15:45 종료 뒤 저녁 수집·Q-7·Q-8 201종목) — **구현 T-035~T-037 작업 지시 대기**. 데이터는 이미 수집됨(실측 결과 K-U42 일부 해소: 005930은 2006-01-02부터 값 있음, 합계 불일치 836행 원인 미확인).
3. **분봉 매일 축적**(A-095 ②) — 대상 종목 목록을 전략실이 정하면 `fetch-minute.py` 저녁 실행 안내(첫 실행 종목당 약 3.5분). 자동 운영 단계에 넣는 것은 CP-029 범위 변경 → 별도 확인.
4. **CP-034 후속**(구현 대기): Q-10 (가)=아침 06:00 이후 run 전 지수 수집(A-094) — 자동 운영 절차 단계 추가 + 배분 규칙 PAPER 배포 연결(`[[allocations]]`) 미구현; 전략 명세 `policy_profile` 선언 검사 미구현. (문서 반영은 A-102로 완료)
5. **CP-032 Q-3**(모의 지정가 실측: 500원 배수 접수·250원 거부, PAPER 주문)·Q-4(단타 프로필 값)·Q-5(다종목 CP).
6. **M2 다음**: T-M2-15 M2 PAPER-0(테스트넷 모의 주문)은 검증 전략 승인과 함께 별도 승인. V-157 미실행 4개(V-02·V-76 부분, V-112·V-127).
7. **문서 묶음 승인 완료(A-102)**. 남은 것: 01 K9 교체 행 vs 04 V-180 충돌 결정, 검토 요약 §6 미결(문서에 착수 시점 표시), `docs/capabilities.md`를 CP-024 능력 표 draft(changes/CP-024-capabilities-draft.md, 조건 "00 v0.5 승인" 충족)로 교체할지 — 실행 코드가 읽는 파일이라 run 꺼진 저녁에, 별도 지시로.
8. 분봉 전략 검증 기준(제공 약 1년이라 CP-030 7창 불가) — 분봉 전략 후보가 나올 때(A-095 ③ 보류).

## 에이전트가 사용자에게 아직 못 알린 것
- T-028 구현 [제안] 8건(15:30 동시호가 봉 제외로 1분봉 합 ≠ 일봉 가능, 30~60분봉 마감 걸친 봉 제외, 부분일 제외, 기존 봉 값 다르면 유지·건수 기록, 분봉 품질 K10 해제 조건 미정, 도구는 CSV만 기록·DB 적재 없음 등 — worklog (44)).
- CP-034 구현 [제안] 6건(CP-034 §8.2): 배분 규칙은 증액도 차단, 일부 전략만 `l6_slots` 주면 시작 거부 등 — A-094로 사용자 확인 완료.
- 키움 모의: **NXT·시간외 미지원**(K-U44·K-U45, 사용자 확인). PV-13 NXT·SOR 대조는 LIVE 전 필수로 이관.

## 브랜치·worktree 정리 — 2026-10-14 완료 (사용자 "정리도 수행")
- 병합 끝난 worktree 37개 제거(`git worktree remove`, 강제 옵션 없음, 거부 0). 남은 worktree: main, `m2-binance`(m2/run).
- 브랜치 삭제: `integ/backtest-m2`(cb698ed, main 대비 +4), `tmp/merge-dryrun`(09f80e0, +3), `feat/T-PV11-token-audit`(병합됨). 병합된 옛 브랜치 이름 약 115개는 남겨 둠(이력은 main에 있음).
- push는 권한 설정(`.claude/settings.local.json`)이 막음 — 사용자가 `git push origin main` 실행(main은 origin보다 311개 앞섬, 이력 비밀값 검사 통과).
- `m2-binance`(m2/run)와 `strategy-lab`(`C:\AI\strategy-lab`, 별도 저장소, 다른 세션이 작업 — 이 세션은 읽기만, 커밋 없음)는 정리 대상 아님.

## 전략실 (2026-10-09 생성, A-085) — `C:\AI\strategy-lab`, 별도 git 저장소(커밋 없음 — 사용자 지시 시)
- 만든 것: `CLAUDE.md`(금지: 브로커 연결·주문·비밀정보·운영 저장소 쓰기·평일 06:40~16:00 무거운 실행), `README.md`(복사용 명령), `tools\sync-data.ps1`(운영 일봉 30종목 복사, **2026-01-01 이후 행 잘라 냄**, 휴장일 JSON 복사), `tools\validate.ps1`(운영 .venv + PYTHONPATH로 운영 `scripts\validate-strategy.py` 실행, 결과는 전략실 `results\`, 평일 낮 거부·휴장일 `-Holiday`), 견본 `candidates\example_sma\`(strategy.py·params-v1.toml·strategy.md·notes.md). .ps1은 UTF-8 BOM(PS 5.1 한글).
- **확인된 사실(2026-10-09 실제 실행)**: 절대 경로 params·out-root·전략실 모듈 import 동작, 운영 저장소 변경 0. 견본 91회 실행 약 86분(시험과 동시), 판정 미충족(견본이라 무의미).
- 아래는 생성 전 설계 기록(참고).
- 목적: 사용자가 실전용 전략을 만들 단계(PAPER-0 무인 검증 뒤 PAPER-1 필요). 연구(많이 만들고 버림)와 운영(승인된 것만)을 분리.
- 위치·구조(제안): 별도 git 저장소 `C:\AI\strategy-lab\` — `CLAUDE.md`(가벼운 규칙: 브로커 연결·주문·비밀정보·운영 저장소 쓰기 금지), `ideas\`(아이디어·다른 AI와 만든 초안), `research\`(노트북·pandas 자유), `data\`(일봉 CSV 사본, git 제외 — 원본 trading_system `var\backtest\data\kiwoom-ka10081-adj-20261007`), `candidates\<전략ID>\`(strategy.md 양식, `<id>.py` Strategy 규격, params.toml), `results\`.
- 미리 돌려 보기(운영 엔진 재사용, 확인된 사실): `scripts\validate-strategy.py`는 `--strategy 모듈:클래스`(PYTHONPATH로 전략실 경로 추가), `--params`(저장소 기준 경로지만 절대 경로도 pathlib상 가능), `--data` 임의 폴더, `--out-root` 지정 가능. 예: `$env:PYTHONPATH='C:\AI\trading_system\trading_system\src;C:\AI\strategy-lab'` 후 운영 .venv python으로 실행, 결과는 전략실 `results\`. **생성 때 실제로 1회 돌려 확인하고 전략실 스크립트로 고정**할 것(미검증: 절대 경로 params, --out-root 절대 경로, 장부 경로). 2026 이후 데이터는 자동 거부. 장중 실행은 피한다(같은 PC 자원).
- 공식 판정·적용(운영 저장소): 후보 3파일을 `src\trading_system\strategy\`·`config\strategies\`·`docs\strategies\<ID>\`로 복사(브랜치) → 구조 시험(전략은 표준 라이브러리만, 네트워크·파일 금지) → validate-strategy 공식 판정표(CP-030 C-1~C-8) → `strategy.py register` → BACKTEST_PASSED(A-ID) → 사용자 승인 PAPER → `config\app.paper.toml` `[[strategies]]` 추가 → PAPER-1.
- 제약: 계좌 한도(L1~L7: 보유 3·일 5건·1회 100만 원)는 전략 공유. 지원 능력 밖이면 CP-032 해당 단계가 먼저. 검증용 paper0-verify는 PAPER-0 종료 뒤 교체.

## 현재 상태 요약
- **M1**: 6단계, PAPER-0 **조건부 통과**(A-097, 5거래일·무인 2일), 왕복 4건, 결과 불명·불일치·중복 0. 전략 paper0-verify v2 PAPER(v1 STOPPED REPLACE, A-067). PAPER 수수료 0.35%(A-075). 시계 장중 최대 0.355초(허용 1초). PAPER-1은 실제 전략 결정 뒤(D-076).
- **M2(바이낸스)**: 설계 CP-024 v0.5, T-M2-01~14 오프라인 구현 main 병합. 테스트넷 연결 승인 A-096(조회만), 유니버스 BTCUSDT, 키 입력·접속 스위치 **사용자 로컬 작업 대기**. 실전 주문 코드·LIVE 승인 없음.
- **문서**: 승인 CP-026~CP-030·CP-031(위임)·CP-032 단계 1·2(A-085·A-095)·CP-034(A-092~A-094). 미승인: CP-025·CP-033. 00~05 draft에 `[CP-025]`~`[CP-032 단계 1]` 표시 반영, `[CP-032 단계 2]`·`[CP-034]`는 미반영.

## 로드맵 (승인 단위)
| 순서 | 작업 | 선행·승인 |
|---|---|---|
| 1 | 매일 무인 자동 운영 유지(PAPER-0 건강 검진) | A-097 |
| 2 | M2 테스트넷 시작 점검(조회만) → 결과로 PV-M2-03 기록 | 사용자 로컬 키 입력 |
| 3 | PV-11 주말 장시간 run(10-16 금 저녁 ~ 10-18 일 밤) | A-097 ④·A-099, 절차 v0.2 |
| 4 | 외부 수동 매매 감지 확인(10-14 미실행 → 날짜 다시 정함) | 사용자 |
| 5 | **실제 전략 결정(전략실)** → T-018 공식 판정(CP-030) → T-019 PAPER-1 | 사용자 |
| 6 | CP-033 승인 → 수급 데이터 판정 입력(T-035·T-036) | 사용자 |
| 7 | CP-034 후속: 아침 지수 수집·배분 규칙 PAPER 연결(T-042 연결), policy_profile 검사 | 구현 대기 |
| 8 | CP-032 Q-3(모의 지정가 실측)·단계 2 T-029·분봉 축적 | 사용자 |
| 9 | M2 PAPER-0(T-M2-15, 검증 전략 승인 포함) | 별도 승인 |
| 10 | LIVE 준비(시장별, 04 §5.1 C-01~C-14, NXT·SOR·시계 0.5초 기준·L1 여유 근거 등 — "LIVE 전 필수" 참조) | 별도 승인(실전 주문 코드·LIVE 실행은 별개) |

## 도구 (사람 실행, 먼저 `cd C:\AI\trading_system\trading_system`)
- 자동 운영 시작(수동 1회) `.venv\Scripts\python scripts\autopilot.py --mode PAPER` / 오늘 run 끝내기 `.venv\Scripts\python scripts\ops-stop.py --mode PAPER`
- 예약 작업 등록·해제(관리자 PowerShell) `powershell -NoProfile -ExecutionPolicy Bypass -File .\scripts\schedule-register.ps1` / `...\schedule-unregister.ps1`
- 점검 `$env:TRADE_MODE='PAPER'; .venv\Scripts\python scripts\preflight.py` (run 실행 중에는 거부) / 수동 실행 `$env:TRADE_MODE='PAPER'; $env:PYTHONPATH='src'; .venv\Scripts\python -m trading_system.app run`
- `.venv\Scripts\python scripts\<도구>.py --mode PAPER`: `status`, `resume K8|K4|K1`, `kill`, `reconcile-resolve`, `confirm-unknown`(결과 불명 확정: `--list` / `<의도ID> --not-accepted` / `<의도ID> --accepted --order-no <번호>`, CP-031 K3 해소), `paper-review`, `telegram-test`, `backup`, `restore-test`, `strategy show|register|transition`, `watchdog`, `migrate`, `archive-logs`
- 데이터·백테스트: `scripts\fetch-daily.py`(키움 일봉 조회, run 종료 뒤에만 A-073 E), `scripts\validate-strategy.py`(CP-030 검증 실행기, main. 2026-01-01 이후 데이터는 `--final-check`로 버전당 1회만; 백테스트는 실행마다 trades.csv·equity.csv·summary.json), `python -m trading_system.app backtest ...`
- 실측·수집 도구(모두 조회만, PAPER run·자동 운영 꺼진 장외에만, 쉼표 목록은 큰따옴표): `kiwoom-probe.py --mode PAPER --codes "005930,000660,069500"`(분봉·틱·WS), `rate-probe.py --mode PAPER`(1700 유도, 실행 완료), `investor-probe.py --mode PAPER [--codes "005930" --deep | --collect --codes-file <목록> --since 2016-01-01]`(수급 ka10060), `fetch-minute.py --mode PAPER --codes-file <목록> --minutes 1 --since 2025-10-01 --out <폴더>`(분봉 축적), `fetch-daily.py ... --kospi200 --index 001`(코스피200·지수), `universe.py submit|show --mode PAPER --list <ID> --file <csv>`(U-B 목록, 로컬만), `binance-preflight.py --market M2`(m2-binance 폴더에서, 테스트넷 조회만).
- 에이전트 대화 지시 실행 범위(A-079): 시작·정지·재개 명령 기록·status·paper-review·backup만. LIVE·실전 주문·불일치 해소 방법 선택·지시 없는 재개 금지.
- 권한 `.claude/settings.local.json`: push·reset·rebase·clean·pip install·비밀 파일 접근·`rm -rf` 차단. **명령문에 비밀 파일 이름이 들어가면 거부**(스크립트 파일로 우회하지 않음).

## 남은 위험·주의
- 재부팅: 10-14 실제 발생. A-098 뒤에도 23:00~05:00 업데이트 재시작은 가능 — 자동 로그인이 안 되면 다시 미실행(06:50 텔레그램 부재로 확인). 에이전트는 자동 로그인 설정을 레지스트리로 확인하지 못함.
- 무인 운영: 자동 운영 프로세스 자체가 죽으면 그날 종료·요약 없음(저녁 요약 부재로 확인), 설정 오류로 텔레그램 못 쓰면 알림 없음(06:50 "run 시작" 부재로 확인), 작업 스케줄러 11시간 상한 시 자식 run 동반 종료 여부 미확인, Interactive 작업이라 로그아웃·재부팅 뒤 미실행.
- 남은 낮음(기록): 무인 운영일 엔진 일일 보고(16:00) 미생성(텔레그램 요약은 정상), 날짜·시각 모두 읽을 수 없는 DAILY_BASE 손상 행은 매일 매수 차단(복구 절차 설명서 부록 A, 해소 코드 없음), confirm-unknown 목록의 immutable 읽기, 429 시 requests 시간 초과가 전체 한도가 아님.
- 07:53 K5는 **5거래일 연속 재현**(모의 서버) — CP-028 P4로 07:58 해제 확인. 텔레그램 429 상한 이내 지연은 K11 없음, 내용 거부 긴급 알림은 로컬만.
- K-U14 행 단위 미확인(부분 체결 여러 번인 날) → K4 보수적. 미구현: 정정(CAP-ORD-03), 분봉, 이벤트 트리거. CP-031 적용 전까지 M1 결과 불명 5회 미발견은 자동 미접수 종결(현 main).
- **문서 편집 주의**: heredoc·Python 문자열에 `scripts\resume.py` 같은 경로를 쓰면 `\r`이 줄바꿈으로 깨진다. 경로가 든 문서 수정은 Edit/Write 도구로, 수정 뒤 `grep -nP "[\x00-\x08\x0b\x0c\x0e-\x1f]" docs -r` 검사. `docs/capabilities.md`는 실행 코드가 읽으므로 PAPER 중 직접 수정 금지.
- 정리 필요(사용자): `C:\Users\user\AppData\Local\Temp\v91-p8kqynon` 임시 폴더.

## LIVE 전 필수
- 기준: **04 §5.1 C-01~C-14 체크리스트 + 07-paper-review §13 현재 상태표**(시장별 판정). 아래는 거기 없는 추가 항목만.
- 달력 KRX 원문 대조 + 사용자 검증 데이터(A-037), 2026-11-19 수능일 KRX 공지 확인.
- **시계**: 모의 운영 기간 장중 최대 오차 기준(제안 0.5초 이하) 충족 — Windows 동기화 주기 조정 등(2026-10-11 관찰 시작). M2는 바이낸스 -1021 때문에 더 엄격히.
- 실전 API 호출 제한·실전 수수료 요율(A-075: LIVE 준비 때 별도 설정)·호가 단위(X-U1) 공식 원문.
- 자동매매 전용 실계좌(A-051), 백업 외부 저장소(A-039), 노출된 모의 키 재발급, 밤샘 실행 검증(PV-11), 작업 스케줄러 상한 동작 실측.
- **PAPER-0에서 모의로 못 본 것(A-097·§12E)**: PV-13 NXT·SOR 외부 주문 대조(모의 NXT 미지원 K-U44, 실전 계좌 소액으로), PV-04 본시험(주문 직후 네트워크 단절 — 평일 09:30 PC 앞에서 사용자 참여), PV-11(주말 run 결과에 따라), PV-01 재정의 CP. 지정가·정정(PV-03·09·14·17)은 CP-032 Q-3·단계 5 뒤.
- **한도·시계**: L1 여유 3% 근거 실측(갭 분포·시장가 체결 불리분, CP-034 D-195), 모의 운영 장중 시계 최대 0.355초(기준 제안 0.5초) 유지 확인, 바이낸스 테스트넷에서 -1021 없음 확인.
- **데이터**: 수급·지수 판정 입력은 모의 서버 데이터가 실제 KRX 집계와 같은지 미확인(실전 데이터 대조), 생존편향(현재 코스피200 구성)·분봉 제공 약 1년.
- 자동 운영은 PAPER 전용 — LIVE 자동화는 별도 CP·승인 없이는 없음. 실전 주문 코드 승인·LIVE 실행 승인(별개, 사용자).

## 작업 방식
- 테스트 통과 → 로컬 커밋 → 이 파일 갱신, worklog 항목 15줄 이내. 문서 커밋 메시지 `docs: ...`.
- 수정 뒤 독립 재검토 1회 이상. 재검토 지적은 코드로 사실 확인 후 반영(틀린 지적 있음).
- 병렬 에이전트는 읽기 전용 또는 파일 소유 분리, 커밋은 리드만. 다른 에이전트 소유 파일(broker-specs 등)은 건드리지 않는다.
- 실행하지 않은 시험을 실행했다고 쓰지 않는다. 3일차 수치처럼 보고 기반이면 [보고]·미확인 표시.
