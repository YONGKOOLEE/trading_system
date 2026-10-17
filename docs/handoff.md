# 세션 인계 (짧게 유지 — 작업이 끝날 때마다 덮어쓴다)

- 갱신: **2026-10-18 03시 (세션 49 끝)**. 마지막 승인 기록 **A-118**(다음 A-119). main **8a010c0 이후**, origin보다 44커밋 이상 앞섬 — **push는 사용자 실행**(`git push origin main`, 에이전트 권한 차단).
- 실전 주문 코드·LIVE 승인 **없음**(M1·M2 모두).
- 방향 전환: **M2(크립토) 우선 인도**(CP-037, A-113). M1은 매일 06:45 무인 건강 검진 유지, M1 PAPER-1은 사용자 전략 결정 대기.

## 지금 돌고 있는 것 (10-18 기준)
| 항목 | 상태 |
|---|---|
| M1 PV-11 주말 run | `main` 체크아웃에서 사용자가 10-17 17:26 재시작(수동 run, **K8 유지 — 주문 0**). 정지 예정 **10-18(일) 21:00~22:30**. 토큰 갱신(TOKEN_ISSUED REFRESH) 예상 일 17:16 전후 |
| M2 T-050 관찰 run | `C:\AI\trading_system\m2-binance` 브랜치 **m2/run2**(=feat/T-049-m2-ops 406ab6a)에서 사용자가 10-17 20:27 시작. 주문 스위치 꺼짐(orders_enabled=false), 배포 전략 없음. **HMAC 사용자 스트림 구독 성공**(binance-spot §13). K8·K4(지급 BTC 1·USDT 기준선 없음 — 예상) |
| 운영 상태(M1) | K4 해소 완료(10-17 17:31), 정지는 K8·K9만, 보유 005930 1주(v2 소유, 월요일 14:30 매도 예정), 열린 불일치 0. 0056724 EXTERNAL "미체결" 표시만 남음(fix-external-order로 정리 예정) |

## 다음 세션 첫 행동 — 오늘 밤 사용자 "PV-11 정지" 뒤 (월요일 06:45 전 끝낼 것)
1. **정지·기록**: `.venv\Scripts\python scripts\ops-stop.py --mode PAPER` → `backup.py`·`paper-review.py --mode PAPER`(A-079 범위) → `status.py`로 정지 K8·K9만 확인 → DB 읽기 전용으로 `audit_event` `TOKEN_ISSUED` 행(INITIAL·REFRESH) 확인 → **07 §12H**에 PV-11 결과(갱신 성공 여부·시계·K5 07:53 주말 재현·자원) 기록. 절차서 `docs/strategies/paper0-verify/pv11-weekend-run.md` §8.
2. **병합은 통합 브랜치에서** (main을 직접 되돌리는 파괴적 명령 피함): `git checkout -b integ/2026-10-18 main` →
   - ① `feat/T-051-m2-minute`(040cf59) — T-045~T-049·m2-verify·M2 백테스트·T-051 체인 전부 포함(406ab6a→abbdda1→45348bc→f785212→040cf59)
   - ② `feat/btc-trend-v1-review`(66d2781, 45348bc 위)
   - ③ `fix/external-resolve`(3a2b23e, main e2eea7b 위)
   - ④ `feat/a110-binance-klines`(e9a3935 — scripts\fetch-binance-klines.py, 이동 뒤 전체 시험 일부만 돌았음)
   - **⑤ `feat/T-052-053-m2-periodic`(434211a)는 이번에 넣지 않음** — 독립 재검토 미실시 + DP-78~DP-81 사용자 확인 필요(아래 결정 대기).
   - 충돌 예상: `docs/decisions-pending.md`(main DP-50 / 체인 DP-51~77 — 행 모두 보존), `docs/capabilities.md`(체인의 M2 열·CAP-INS-05 — M1 칸 불변 확인). 그 밖은 수동 해소 후 사실 확인.
   - 통합 브랜치에서 **전체 시험** `.venv\Scripts\python -m pytest -q`(-m 없음, 약 25분). 부하 민감 시험 2개(`test_run_rehearsal2::test_r3_soak_market_hours_no_false_halts`, `test_t049_review_fixes::test_dead_binance_feed_and_user_stream_threads_raise_k5`)만 실패하면 단독 재실행. **통과하면** `git checkout main; git merge --ff-only integ/2026-10-18`. 실패하면 main은 그대로(월요일 운영 보호) 두고 원인 조사.
3. **사용자 실행**(병합 뒤): `& C:\AI\trading_system\trading_system\.venv\Scripts\python.exe C:\AI\trading_system\trading_system\scripts\fix-external-order.py --mode PAPER --account KR-PAPER-01 0056724` → 기대 "정정 완료 … ACCEPTED 0/1 → FILLED 1/1" → status 미체결 0. (운영 DB 0056724 근거 항목은 "BUY 1 FILLED" 1건 — 10-17 읽기 전용 확인)
4. **M2 준비**(사용자와 함께, M2 관찰 run을 Ctrl+C로 멈춘 뒤): m2-binance를 새 main으로 — `git -C C:\AI\trading_system\m2-binance checkout -b m2/run3 main`(사용자 로컬 수정 `config/m2/app.paper.toml` connection_approved=true가 그대로 따라오는지 확인, 바뀌면 사용자가 다시 켬) → m2-verify 등록·전환(전환 승인 **A-112**, 절차 `docs/strategies/m2-verify/strategy.md` PV-M2 절차 7단계: register → BACKTEST_PASSED(증거 backtest-v1.md) → PAPER) → 기준선 `reconcile-resolve.py initial-baseline --mode PAPER --market M2` → M2 텔레그램 키(사용자가 BotFather로 생성, m2-binance 자격정보 파일에 `M2_TELEGRAM_BOT_TOKEN`·`M2_TELEGRAM_CHAT_ID` 직접 입력, `[telegram] enabled`는 사용자) → **T-M2-15 테스트넷 주문 승인 요청**(지급 BTC 1개 `/exit` 포함 — A-111 Q-3, `orders_approved`·`orders_approval="A-…"`는 사용자가 설정, A-ID는 approvals.md에 있어야 시작됨) → M2 PAPER-0 시작(수동, 출근 전 시작·텔레그램 /resume K8, A-106 Q-2).
5. 월요일 06:50 M1 텔레그램 "시작 점검 통과"·"K8 자동 재개 완료" 확인(사용자). 첫 실행 코드가 병합본이면 특히.

## 구현 대기열 (승인됨, 오프라인부터)
| 순서 | 작업 | 근거 | 상태 |
|---|---|---|---|
| 1 | T-052·T-053 **독립 재검토** → DP-78~81 확인 → 병합 | CP-037, A-113·A-114 | 구현 완료(e443287·434211a, 전체 3315 passed). worktree `.claude\worktrees\agent-aa43e5c9e4d9e55bd` |
| 2 | T-054 M2 무인 감시 프로세스·T-055 예약 작업 스크립트 | CP-037 D-223~D-231, A-113 Q-1 (가) 로그온 시 작업 스케줄러·Q-2 (나) 제한 재시작 | 미착수 (btc-trend는 일봉이라 무인 운영이 분봉 판정보다 먼저 필요) |
| 3 | CP-040 T-065 수수료 필드 → T-066 1h/15m 집계·확장 열 → T-067 1분 손절 감시 → T-068 백테스트 확장 → T-069 문서 | A-115 (Q-1 60·15분, Q-2 시스템 1분 감시, Q-3 1분봉 저가 L4, Q-4 L5 5, Q-5 0.1% 자리값) | 미착수 |
| 4 | CP-039 T-060~T-063(신호=실서버 공개 시세, 주문=테스트넷, D-098 좁게 개정, 가격 차 1% 진입 거부) → T-064 관찰은 **별도 연결 승인** | A-115 | 미착수 (CP-040과 같은 경로라 동시 진행 금지) |
| 5 | T-056 M2 무인 관찰 run(주문 차단) | 별도 승인 | — |

## 전략실 요청 A~E (2026-10-18, 세션 메시지 strategy-lab-46) 처리 결과 — 승인 A-118
| # | 요청 | 결과 |
|---|---|---|
| A | crypto-trend v1(BTC·ETH 한 계좌, 60분 판정, 진입 게이트 가격>SMA50) 검토 | 브랜치 `feat/crypto-trend-v1-review` **9fdb517**(434211a 위, worktree `.claude\worktrees\agent-a7209a7812a5d0e6b`, 전체 3335 passed/3 xfailed). 원본 바이트 동일 복사, 능력 CAP-TRG-02·DAT-01·ORD-01·INS-05. BACKTEST 등록 통과·PAPER 거부(예상)·M2 조립은 ETHUSDT 유니버스 밖이라 거부. 결함 F-1(period_minutes 없음)·F-2(validate_deploy 23개 vs D-219 eval_times=[])·F-3(validate_rules에 종목 없음). **재현 불가** — T-068에 여러 심볼·일봉 입력 필요. 근사 1회(BTC만·일봉 근사) 27건·+7.78%. 리뷰 `docs/strategies/crypto-trend/review-v1.md`. 전략실 참고: 2018 상위 18종목에서도 추세 효과(비율 0.99) — BTC·ETH 수치(약 2)는 과대 가능 |
| B | M2 유니버스 ETHUSDT 추가 | **사용자 결정 대기**(제안서 `C:\AI\strategy-lab\research\proposal-ops-eth-expansion-2026-10-18.md`) |
| C | USDT 현물 전 심볼 일봉 | **완료**: 765심볼(TRADING 511·BREAK 254) 중 763 수집·851,978행·143MB `var\backtest\data\binance-public-usdt-1d-20261018\`(meta\summary.csv), 실패 2(비ASCII 심볼명), 옛 폐지 표본 17개 모두 목록에 있음. 전략실 전달·수령 확인 |
| D | 저빈도 코인 전략 PAPER-1 90일 이상 | **사용자 결정 대기**(CP-037 D-232는 28일, CP-038 초안은 90일) |
| E | 코스피200 ETF 일봉·M1 ETF 매매 | 답함: ETF 일봉은 ka10081로 가능성 높음(미실측), M1 ETF 매매는 CAP-INS-02 미지원 → CP 필요 |

## 사용자 결정 대기
0. **crypto-trend 후속(요청 A)**: F-1·F-2는 **전략실이 v2로 수정 완료**(CryptoTrendV2, period_minutes 60, eval_times 빈 목록, 매매 규칙 불변 — 전략실 커밋 a5c1405, 운영 재검토는 사용자 승인 뒤). 테스트넷 ETHUSDT LOT_SIZE 0.0001 확인(binance-spot §15), T-068 범위에 여러 심볼 한 계좌·확정 일봉 입력(1d 파일 vs 1분봉 집계) 명시할지(CP-040 개정), F-3 해결 방식(DP-54 서명에 종목 추가 공통 CP 권장 / 전략 파라미터). B·D도 함께.
1. **T-052·T-053 DP-78~81**: DP-78 분봉 무효 시 ENTRY·INCREASE만 버리고 EXIT·REDUCE 허용(§8-3), DP-79 판정 대기·유예(min(60초, N/2), fetch_wait 15/30초, 재시작 뒤 지난 판정 0건), DP-80 capabilities M2 CAP-TRG-02·CAP-DAT-02 "검증 중", DP-81(T-053). 재검토 뒤 확인 요청.
2. **CP-038(저빈도 대체 기준) vs A-091 Q-5 "C-2를 약하게 만들지 않음" 충돌** — 사용자는 전략실 검토 뒤 결정. 전략실 회신: BTC+ETH 2종목이면 82건(C-2를 다종목으로 해결하는 A-091 방향과 맞음) → **전략실 ETH 확대 제안서**(`C:\AI\strategy-lab\research\proposal-ops-eth-expansion-2026-10-18.md`, 사용자가 검토 후 전달 예정)를 먼저 보는 것을 권장. 유니버스 A-096(BTCUSDT만) 변경·한도는 사용자 결정.
3. **btc-trend v1**: 확정 아님(사용자 검토 중). 운영 검토 803c8c5·66d2781 — 재현 일치(40건·+6,498.6%·MDD 43.38%), 판정 초안 "판단 불가(C-2 30건)", F-1 수정·F-2/F-3 보강(A-114 ③), PAPER는 M2 PAPER-0 뒤·능력 "지원" 승격 뒤(A-114 ④), LIVE 한도는 LIVE 준비 때 보수안부터(A-114 ⑤). 전략실 새 후보 `crypto_trend`(1시간 판정, T-066·T-069 뒤 코드).
4. **[M2 LIVE 전] 실전 키 IP 제한**: A-063 유지(고정 IP 중계/통신사 고정 IP) vs 로컬 기간 완화(보완 장치 필수). 선행: 바이낸스 공식 정책 확인. 사용자 장기 방향 클라우드, 공인 IP 변동 확인 중. 결정 시점 T-057.
5. **B-U6/U-17** 계정·약관·법규·세금 확인은 사용자가 LIVE 요청 전(A-113 Q-4).
6. DP-46~DP-77(결정 대기열 `docs/decisions-pending.md`), 데이터 실험 결과: 상장폐지 USDT 심볼 254개(BREAK) 포함 765심볼 일봉 수집 가능(binance-spot §14) — 수집은 전략실 지시·별도 승인.

## 이번 주 승인 요약 (A-098~A-117)
A-098 Windows Update 대책 · A-099 PV-11 절차·토큰 감사 기록 · A-100 CP-033 · A-101 CP-025 · A-102 계획 문서 묶음 재승인 · A-103 R-9 정정 · A-104/A-105 CP-035(보유 중 교체 금지)·T-044 · A-106 CP-036(M2 실행 경로) · A-107/A-108 키움 일봉 수집(사용자 매매·전 종목) · A-109 M2 병합·T-050 연결·M2 텔레그램·EXTERNAL 수정 · A-110 바이낸스 공개 시세(BTC) · A-111/A-112 m2-verify 결정·BACKTEST_PASSED·PAPER 전환 · A-113 CP-037(M2 우선) · A-114 분봉 품질 처리·C-2 B안 방침·btc-trend 결함 · A-115 CP-039·CP-040 · A-116 ETHUSDT · A-117 상장폐지 확인 실험.

## 데이터 (모두 `var\backtest\data\`, git 제외, 읽기 전용 제공)
- `kiwoom-krall-adj-20261017\` 국내 주식 2,771종목 일봉 2016~2026-10-16(+amount, 수정 거래량 확인, README), meta\stocks.csv(ETF·ETN 목록 포함)
- `kiwoom-usertrades-adj-20261016\` 사용자 매매 151+코스피200(351종목)
- `binance-public-20261017\` BTCUSDT·ETHUSDT 일봉(3,348봉)·1분봉(약 481만 행, 빈 구간 35곳) — 실서버 공개 시세
- 정리 대상(사용자): `tmp-index001-20261016\`, `C:\Users\user\AppData\Local\Temp\v91-p8kqynon`
- 백테스트·검증에는 2026-01-01 이후 행 제외(CP-030 최종 확인 구간).

## 전략실 연락
- 이 세션 이름 `trading-system-eb`. 전략실 세션 `strategy-lab-46`(작업 중)에 10-18 수집·판단 결과를 메시지로 보냄 → 회신(참고용, 요청 없음): ETH 재현, 2종목 82건, 1시간 판정 우위·15분 열위, F-1~F-3 원본 반영, ETH 확대 제안서 작성(사용자 검토 후 전달).
- 전략실 저장소는 **읽기만**(쓰기·커밋 금지). 메시지는 `SendMessage`(ListAgents로 이름 확인).

## 도구 (사람 실행, 먼저 `Set-Location C:\AI\trading_system\trading_system`)
- M1: `scripts\autopilot.py`·`ops-stop.py`·`status.py`·`resume.py`·`reconcile-resolve.py`(요청만 기록 — **run이 돌아야 처리됨**)·`confirm-unknown.py`·`paper-review.py`·`backup.py`·`strategy.py` (`--mode PAPER`). 예약 작업 `TradingSystem-PAPER-Autopilot`·`ClockSync`.
- 수집(조회만, run 꺼진 때): `fetch-daily.py`(+`--skip-existing --amount --deadline --unadjusted`), `list-stocks.py`, `resolve-names.py`, 바이낸스 공개 시세 `scripts\fetch-binance-klines.py`(브랜치 feat/a110-binance-klines — 병합 전에는 그 worktree에서 실행).
- M2(m2-binance 폴더): `scripts\run-m2.py`(TRADE_MODE=PAPER), `status.py --mode PAPER --market M2`, `binance-preflight.py --market M2`.
- PowerShell 실행 오류 "module '.venv' could not be loaded" = 현재 폴더가 저장소가 아님 → 전체 경로(`& C:\…\python.exe C:\…\scripts\…`)로 안내.

## 주의·위험
- **서브에이전트 지시에 프로세스 종료 명령 금지**(10-16 taskkill 사고, 피해 없음 — 메모리 규칙). 지시에는 금지 목록(네트워크·.env·main 체크아웃·var·kill·push)을 항상 넣는다.
- PC 부하(전략실 백테스트) 때 타이밍 시험 2개가 실패할 수 있음 — 이 때문에 T-047·T-M2-15 백테스트 커밋이 실패 시험 1건을 안고 커밋된 규칙 위반이 2번 있었음(단독 재실행 통과 기록). 전략실 무거운 실행은 평일 06:40~16:00 금지(전략실 규칙).
- 경로 역슬래시: heredoc·Python 문자열에 `\r`·`\a` 등이 깨짐 → 경로가 든 문서는 Edit/Write 도구로, 수정 뒤 `grep -nP "[\x00-\x08\x0b\x0c\x0e-\x1f]" docs -r`.
- `docs/capabilities.md`는 실행 코드가 읽음 — M1 run 중 변경 금지(병합도 run 꺼진 때).
- 모의 서버: 매일(주말 포함) 07:53 서버측 WS 종료 → K5 → 대조로 해제. 키움 모의 NXT·시간외 미지원.
- 재부팅: A-098 뒤에도 23:00~05:00 업데이트 재시작 가능(자동 로그인 의존). PV-11 동안 Windows Update 1주 일시 중지(사용자).
- 테스트넷 시세·체결은 실제 시장과 다름(B-U4) → CP-039로 신호는 실서버 공개 시세 예정.

## LIVE 전 필수 (요약 — 상세 04 §5.1 C-01~C-14, 07 §13, CP-037 D-233 L-1~L-10)
- M1: 달력 원문 대조, 시계 0.5초 기준, 실전 호출 제한·수수료·호가 단위 원문, 전용 실계좌, PV-13 NXT·SOR·PV-04 본시험, L1 여유 근거, 모의 데이터와 KRX 대조.
- M2: B-U6 계정·약관, 실전 키 IP 제한 결정, 실서버 수수료·호출 제한, 테스트넷 체결 품질 한계, 실전 주문 코드 승인·LIVE 실행 승인(각각 별개, 사용자).

## 작업 방식
- 시험 먼저 → 통과 → 로컬 커밋(push·main 병합은 지시 시) → 이 파일·worklog(15줄 이내) 갱신.
- 수정 뒤 독립 재검토 1회 이상, 지적은 코드로 사실 확인 후 반영. 서브에이전트 결과의 수치는 원본 1~2곳 직접 대조(CLAUDE.md §12).
- 승인은 사용자가 대화에서 한 것만 approvals.md에 전사. 승인 범위가 모호하면 확인(AskUserQuestion이 유용).
- 사용자: 터미널 비숙련 — 복사해 붙일 전체 명령·기대 결과, 용어 풀어서, "실전"은 LIVE에만. 평일 07:15 출발·장중 부재·텔레그램만.
