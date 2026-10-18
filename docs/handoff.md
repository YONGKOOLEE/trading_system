# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: **2026-10-18 (세션 49 끝)**. 마지막 승인 기록 **A-121**(다음 A-122). main은 origin보다 50커밋 넘게 앞섬 — **push는 사용자가 실행**(`git push origin main`, 에이전트는 권한 차단).
- 실전 주문 코드·LIVE 승인 **없음**(M1·M2 모두).
- 방향: **M2(크립토) 우선**(CP-037, A-113). **M2 매매 대상은 BTC·ETH 고정**(A-120 — 거래 수 때문에 종목을 늘리지 않음, 전략실 v3 coin-top5-trend는 보류). M1은 매일 06:45 무인 건강 검진만 하고, M1 PAPER-1은 사용자 전략 결정 대기.
- 저빈도 전략 판정 경로 확정: **CP-038 v0.2 승인(A-121)**. A-091 Q-5를 M2에서만 변경 — 거래 수(C-2) 하나만 모자라면 대체 증거 LF-0~LF-8로 판정. LF-8은 다른 코인 29개에 같은 규칙을 적용하는 필수 보조 증거. 조건: PAPER-1 90일 이상 + 왕복 1건(상한 180일), LIVE 첫 규모는 일반의 50% 이하.

## 지금 돌고 있는 것
| 항목 | 상태 |
|---|---|
| **LF-8 표본 1분봉 수집**(A-121) | **완료** 10-18 10:25~15:09, 29심볼 모두 성공·실패 0, 약 9.8GB, `varacktest\datainance-public-lf8-1m-20261018\`(meta\README.txt). 2025-12-31 전에 데이터가 끝난 4개(EOS·BTT·MATIC·FTM)는 이름 변경·토큰 전환 추정 — D-244 (4) '데이터 끝 = 종가 × 0.9 청산'이 과하게 불리할 수 있어 T-073 때 사용자 확인 |
| M1 PV-11 주말 run | `main` 체크아웃, 사용자가 10-17 17:26 재시작(수동, **K8 유지 — 주문 0**). 정지 예정 **10-18(일) 21:00~22:30** |
| M2 T-050 관찰 run | `C:\AI\trading_system\m2-binance` 브랜치 m2/run2(406ab6a), 10-17 20:27 시작. 주문 스위치 꺼짐, 배포 전략 없음, HMAC 사용자 스트림 구독 성공 |
| M1 운영 상태 | 정지 K8·K9만, 보유 005930 1주(v2 소유, 월요일 14:30 매도 예정), 열린 불일치 0. 0056724 EXTERNAL "미체결" 표시만 남음(아래 3) |

수집 재실행 명령(필요할 때만, 이어 받기):
`& C:\AI\trading_system\trading_system\.venv\Scripts\python.exe -I C:\AI\trading_system\trading_system\var\backtest\data\binance-public-lf8-1m-20261018\meta\lf8_collect.py C:\AI\trading_system\trading_system\var\backtest\data\binance-public-lf8-1m-20261018\meta\lf8-sample-list-for-collection.csv`
(스크립트가 쓰는 도구는 worktree `.claude\worktrees\agent-aceeedf51e572751f`에 있음 — 병합 ④ 뒤 worktree를 지우면 스크립트의 TOOL 경로를 `scripts\fetch-binance-klines.py`로 바꿔야 함)

## 다음 세션 첫 행동 — 오늘 밤 사용자 "PV-11 정지" 뒤 (월요일 06:45 전에 끝낼 것)
1. **정지·기록**: `ops-stop.py --mode PAPER` → `backup.py` → `paper-review.py --mode PAPER` → `status.py`로 정지가 K8·K9뿐인지 확인 → DB 읽기 전용으로 `audit_event` `TOKEN_ISSUED`(INITIAL·REFRESH) 확인 → **07 §12H**에 PV-11 결과 기록(토큰 갱신 성공 여부, 시계, 07:53 K5 주말 재현, 자원). 절차서 `docs/strategies/paper0-verify/pv11-weekend-run.md` §8.
2. **병합은 통합 브랜치에서**: `git checkout -b integ/2026-10-18 main` → ① `feat/T-051-m2-minute`(040cf59, T-045~T-051 체인 전체) ② `feat/btc-trend-v1-review`(66d2781) ③ `fix/external-resolve`(3a2b23e) ④ `feat/a110-binance-klines`(e9a3935). **넣지 않음**: ⑤ `feat/T-052-053-m2-periodic`(434211a — 독립 재검토·DP-78~81 확인 전), ⑥ `feat/crypto-trend-v1-review`(b8fb28c — ⑤ 위에 있음). 충돌 예상: `docs/decisions-pending.md`(행 모두 보존), `docs/capabilities.md`(M1 칸 불변 확인). 전체 시험 `.venv\Scripts\python -m pytest -q`(약 25분, -m 없음). 부하에 민감한 시험 2개(`test_r3_soak_market_hours_no_false_halts`, `test_dead_binance_feed_and_user_stream_threads_raise_k5`)만 실패하면 단독 재실행 → 통과하면 `git checkout main; git merge --ff-only integ/2026-10-18`. 실패하면 main을 그대로 두고 원인을 조사(월요일 운영 보호). (LF-8 수집은 끝났음)
3. **사용자 실행**(병합 뒤): `& C:\AI\trading_system\trading_system\.venv\Scripts\python.exe C:\AI\trading_system\trading_system\scripts\fix-external-order.py --mode PAPER --account KR-PAPER-01 0056724` → 기대 "정정 완료 … ACCEPTED 0/1 → FILLED 1/1" → status에서 미체결 0.
4. **M2 PAPER-0 준비**(사용자와 함께, 관찰 run을 Ctrl+C로 멈춘 뒤): `git -C C:\AI\trading_system\m2-binance checkout -b m2/run3 main`(사용자 로컬 수정 `config/m2/app.paper.toml` connection_approved=true가 따라오는지 확인) → m2-verify 등록·전환(A-112, `docs/strategies/m2-verify/strategy.md` 절차) → `reconcile-resolve.py initial-baseline --mode PAPER --market M2` → M2 텔레그램 키(사용자가 BotFather로 만들어 자격정보 파일에 직접 입력) → **T-M2-15 테스트넷 주문 승인 요청**(지급 BTC `/exit` 포함, `orders_approved`·`orders_approval`은 사용자가 설정) → M2 PAPER-0 시작.
5. 월요일 06:50 M1 텔레그램 "시작 점검 통과"·"K8 자동 재개 완료" 확인(사용자).
6. 수집 결과는 확인·보고 완료(README 작성). 수집용 목록은 공식 목록이 아님 — T-073 공식 목록과 다르면 보충 수집.

## 사용자 결정 대기
1. **CP-041**(M2 BTC+ETH 2종목, draft v0.1 — `docs/changes/CP-041-m2-multi-symbol.md`): 승인 여부와 Q-1 한도(권장: A-064 그대로)·Q-2 슬롯(권장: 지금 정하지 않음)·Q-3 일봉 출처(권장: 거래소 1d 파일)·Q-4 저빈도 기준(권장: 28일 창 왕복 0건 비율 ≥ 25%)·Q-5 ETH 반영 시점(권장: crypto-trend PAPER 준비 때). **10-18 "권장대로"는 CP-038에 대한 답으로 기록함** — CP-041도 같은 뜻인지 확인 필요.
2. **어느 BTC·ETH 전략으로 갈지**: crypto-trend v2(60분 판정 — T-071 확장 + LF-8 1분봉, 지금 수집 중) vs 일봉 2종목판(현재 엔진으로 가능하지만 M2 일봉 6창 검증 경로가 없음 — T-058 범위). 전략실 판단: 1시간 판정 우위.
3. **CP 작업 착수 지시**: CP-038 T-058·T-059·T-073 (CP 승인 ≠ 착수 지시).
4. T-052·T-053 DP-78~81 — 독립 재검토 뒤 확인 요청(DP-78 분봉 무효 시 매수만 막고 매도 허용, DP-79 판정 시각·유예, DP-80 능력표 "검증 중", DP-81 1분봉 백테스트 세부).
5. 전략실 031890f(주석 1줄)는 다음 복사 때 포함 권장. btc-trend v1은 확정 아님.
6. [M2 LIVE 전] 실전 키 IP 제한(T-057 때): 고정 IP / 직접 만든 키 유형 + 보완 장치(공식 문서 재확인 필요). B-U6·U-17 계정·약관·세금은 사용자가 LIVE 요청 전에 확인.
7. DP-46~DP-77(`docs/decisions-pending.md`).

## LF-8 수집용 목록 처리 (T-073 확정 때 사용자 확인 필요 — CP-038 D-244 해석)
- 29심볼: EOS·BNB·XRP·NEO·LTC·ADA·TRX·ETC·ONT·XLM·BTT·LINK·MATIC·BCH·YFI·DOGE·DOT·SHIB·SOL·GMT·APE·FTM·AVAX·ARB·APT·OP·PEPE·WIF·SUI (USDT 쌍).
- 규칙 외 해석 3가지: ① **직전 해 말에 이미 거래가 중단된 심볼 제외**(1월 1일에 고를 수 없음 — BCC 2019·BCHABC 2020) ② 레버리지 토큰은 "기초 자산 + UP/DOWN/BULL/BEAR이면서 그 기초 자산이 실제로 있을 때"만 제외(JUP 같은 이름 오판 방지) ③ 제외 표에 금 연동(PAXG·XAUT)·BTC/ETH 포장 토큰(WBTC·WBETH·BETH) 추가. LUNA는 2023 목록에서 연속 7일 결측으로 제외(이름 재사용).

## 구현 대기열 (승인됨, 오프라인부터)
| 순서 | 작업 | 근거 | 상태 |
|---|---|---|---|
| 1 | T-052·T-053 독립 재검토 → DP-78~81 확인 → 병합 | CP-037 | 구현 완료(434211a), worktree `.claude\worktrees\agent-aa43e5c9e4d9e55bd` |
| 2 | T-054 M2 무인 감시·T-055 예약 작업 스크립트 | CP-037 D-223~D-231 | 미착수 |
| 3 | CP-040 T-065~T-069(수수료·1h 집계·1분 손절 감시·T-068 백테스트 확장) | A-115 | 미착수 |
| 4 | CP-039 T-060~T-063(신호 = 실서버 공개 시세) | A-115 | 미착수(CP-040과 동시 진행 금지) |
| 5 | CP-041 T-070(F-3)→T-071(T-068 확장)→T-072 | **CP-041 승인 대기** | — |
| 6 | CP-038 T-073(LF-8 목록)→T-058(저빈도 판정 + LF-8 + 일봉 6창 경로 여부)→T-059 | A-121, **착수 지시 대기** | — |

## 브랜치·worktree 메모
- crypto-trend v2 재검토 `feat/crypto-trend-v1-review` b8fb28c(worktree `agent-a7209a7812a5d0e6b`): 전략실 a5c1405와 바이트 동일, F-1·F-2 해소, F-3 xfail, BACKTEST 등록만, 재현은 T-071 뒤. 리뷰 `docs/strategies/crypto-trend/review-v2.md`.
- btc-trend v1 검토 66d2781: 재현 일치(40건), 판정 초안 "판단 불가(C-2)".

## 데이터 (모두 `var\backtest\data\`, git 제외)
- `kiwoom-krall-adj-20261017\`(국내 2,771종목 일봉), `kiwoom-usertrades-adj-20261016\`(351종목)
- `binance-public-20261017\` BTC·ETH 일봉·1분봉, `binance-public-usdt-1d-20261018\` USDT 763심볼 일봉(상장폐지 포함), **`binance-public-lf8-1m-20261018\` LF-8 표본 1분봉(수집 중)**
- 정리 대상(사용자): `tmp-index001-20261016\`, `C:\Users\user\AppData\Local\Temp\v91-p8kqynon`
- 백테스트·검증에는 2026-01-01 이후 행을 쓰지 않음(최종 확인 구간).

## 전략실 연락
- 이 세션 `trading-system-eb`, 전략실 `strategy-lab-46`. 10-18에 crypto-trend v2 재검토 결과를 보냄. 전략실 v3 인계 패킷(`candidates\coin_top5_trend\handoff.md`)은 A-120으로 보류. 전략실 저장소는 **읽기만**.
- A-120·A-121(BTC·ETH 고정, CP-038 승인, LF-8 표본)은 아직 전략실에 알리지 않음 — 사용자가 전달하거나 다음 세션이 메시지로 보냄.

## 도구 (사람 실행, 먼저 `Set-Location C:\AI\trading_system\trading_system`)
- M1: `scripts\autopilot.py`·`ops-stop.py`·`status.py`·`resume.py`·`reconcile-resolve.py`(요청만 기록 — run이 돌아야 처리됨)·`confirm-unknown.py`·`paper-review.py`·`backup.py`·`strategy.py`(`--mode PAPER`). 예약 작업 `TradingSystem-PAPER-Autopilot`·`ClockSync`.
- 수집: `fetch-daily.py`, `list-stocks.py`, `resolve-names.py`, `scripts\fetch-binance-klines.py`(병합 전에는 worktree `agent-aceeedf51e572751f`).
- M2(m2-binance): `scripts\run-m2.py`, `status.py --mode PAPER --market M2`, `binance-preflight.py --market M2`.
- PowerShell 오류 "module '.venv' could not be loaded" = 현재 폴더가 저장소가 아님 → 전체 경로로 안내.

## 주의·위험
- 서브에이전트 지시에는 프로세스 종료 명령·네트워크·비밀 파일·main 체크아웃·var·push 금지를 항상 넣는다(10-16 taskkill 사고).
- PC 부하 때 타이밍 시험 2개가 실패할 수 있음(실패 시험을 안고 커밋한 위반 2회 있었음). 전략실 무거운 실행은 평일 06:40~16:00 금지.
- 경로 역슬래시는 heredoc·Python 문자열에서 깨짐 → Edit/Write 도구 사용, 수정 뒤 제어 문자 검사.
- `docs/capabilities.md`는 실행 코드가 읽음 — M1 run 중 변경·병합 금지.
- 모의 서버는 매일 07:53 서버측 WS 종료 → K5 → 대조로 해제. 키움 모의 NXT·시간외 미지원.
- 23:00~05:00 Windows Update 재시작 가능(자동 로그인 의존).
- 테스트넷 시세·체결은 실제 시장과 다름 → CP-039로 신호는 실서버 공개 시세 예정.
- 승인 기록 정정: A-119 비고의 ETH 순서 이유는 틀림(실제 이유는 지급 ETH의 K4·한도 영향 — CP-041 D-272), approvals.md 원문은 고치지 않음.

## LIVE 전 필수 (요약 — 04 §5.1, 07 §13, CP-037 D-233 L-1~L-10)
- M1: 달력 원문 대조, 시계 기준, 실전 호출 제한·수수료·호가 단위 원문, 전용 실계좌, PV-13·PV-04, L1 근거, 모의 데이터와 KRX 대조.
- M2: B-U6 계정·약관, 실전 키 IP 제한, 실서버 수수료·호출 제한, 테스트넷 체결 품질 한계, 저빈도 예외 전략은 첫 규모 50% 이하(CP-038 D-239), 실전 주문 코드 승인·LIVE 실행 승인(각각 별개).

## 작업 방식
- 시험 먼저 → 통과 → 로컬 커밋(push·main 병합은 지시 시) → 이 파일·worklog(15줄 이내) 갱신.
- 수정 뒤 독립 재검토 1회 이상. 서브에이전트 수치는 원본 1~2곳 직접 대조(CLAUDE.md §12).
- 승인은 사용자가 대화에서 한 것만 approvals.md에 전사. 범위가 모호하면 확인.
- 사용자: 터미널 비숙련 — 복사해 붙일 전체 명령과 기대 결과를 주고 용어를 풀어 씀. "실전"은 LIVE에만. 평일 07:15 출발·장중 부재·텔레그램만.
