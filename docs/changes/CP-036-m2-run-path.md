# CP-036 — M2 실행 경로: 엔진 조립·바이낸스 테스트넷 연결 (`run --market M2`)

- 버전: v0.1
- 상태: **draft** — 사용자 지시(2026-10-15, "M2 실행 경로 CP 작성")로 에이전트가 작성. **미승인.** 이 문서의 승인은 설계·작업 승인이다. 테스트넷 추가 연결(T-050 관찰 run), 테스트넷 주문 전송(T-M2-15), 실전 주문 코드, LIVE 승인은 **포함하지 않는다**(CLAUDE.md §3 승인 구조). 승인 전에는 승인 기준본 03 v1.0·04 v0.9·05 v0.6(A-102)·`docs/capabilities.md`를 고치지 않는다. 바뀌는 문구는 이 문서 §9에만 있다.
- 작성일 / 최종 수정일: 2026-10-15 / 2026-10-15
- 단계 / 게이트: 6단계(구현) 중 계획 문서 변경 제안 / 대상 게이트 design·validation·plan (+ capabilities.md M2 열, 01 시작 조건 문구)
- 인도 차수: M2. 단, 엔진·조립 지점은 시장 공통 코드라 **M1 회귀 대상**(V-152)
- 선행 문서(버전): 03-design v1.0, 04-validation v0.9, 05-plan v0.6 (모두 A-102 승인본), CP-024 v0.5 (A-066), broker-specs/binance-spot.md §11·§12 (테스트넷 실측 2026-10-14), M2 검증 전략 초안 `docs/strategies/m2-verify/strategy.md` v1 (worktree `.claude/worktrees/agent-a314ab2373f731e73`, 브랜치 `feat/T-M2-15-verify-strategy` 36e4b2d, **미승인·미병합**) "가정·미확인·착수 차단" B-1~B-10
- 근거 승인: A-055(UTC 00:00 일 경계·USDT 기준), A-061(코드 공유 + 실행 분리), A-062(테스트넷·리셋 정지·시장별 정지·순 수령·dust·대조 범위), A-063(IOC 매수·시장가 매도·시장별 봇·HMAC·테스트넷 시세), A-064(M2 PAPER 한도·주문 창 UTC 00:10~23:50·시세 60초·서버 시각 0.5초), A-065(Q1 순서·Q2 체크아웃·Q4 지연 매도), A-066(CP-024 v0.5 승인), A-071(M2 구현 시작), A-096(T-M2-14 테스트넷 연결 **조회만**·BTCUSDT), A-097⑤(M2 체크아웃 갱신), A-102(계획 문서 재승인)
- 새 ID: **D-201~D-216**, **V-280~V-290**, **PV-M2-12**, **T-045~T-050**. (2026-10-15 grep 확인: main·worktree 3개(m2-binance, agent-a314…, agent-a324…)·로컬 브랜치 전체의 docs·src·tests에 D-201 이상·V-280 이상·T-045 이상·PV-M2-12·CP-036 미사용. 마지막 사용 D-200·V-279·T-044·PV-M2-11)
- 관련 기존: CP-024 D-096~D-120, 03 D-038·D-047·D-050·D-052·D-060·D-092·§13·2-M2, CP-026(원격 재개)·CP-029(M1 무인 운영)·CP-035(D-200)·T-044(재판정 방지), V-129~V-161, PV-M2-01~11, T-M2-01~T-M2-17
- 작성 중 실행한 명령: `git status`, `git log`, `git worktree list`, `git branch`, `git grep`, `grep`, 파일 읽기(비밀 파일 제외). **시험·프로그램 실행 없음, 외부 연결 없음, 코드 변경 없음.**

## 1. 문제와 범위

### 1.1 문제
| # | 사실 (근거) | 문제 |
|---|---|---|
| P-1 | `app/main.py:237-245`: `run --market M2`는 `check_m2_startup` 뒤 무조건 `ConfigError("M2 시작 검사 통과 — M2 실행 경로(엔진 조립·바이낸스 연결)는 아직 없다 …")`로 거부 | M2 PAPER-0(T-M2-15)을 돌릴 프로그램이 없다 |
| P-2 | 05 v0.6 작업 표는 T-M2-14(시작 점검·조회만) 다음이 바로 T-M2-15(PAPER-0)이고, 엔진 조립 작업이 없다. T-M2-01~14의 M2 구성 요소는 **시험 조립(`tests/support/m2_fakes.py:180-223`, `scenario_dsl_m2.py`)에서만** 함께 쓰인다 | 승인된 계획에 빈칸이 있다 — 2026-10-15 세션 47에서 "다음 단계 가능"이라고 한 보고는 오류였다(worklog) |
| P-3 | 03 v1.0 §2-M2.1 도식은 `Engine ─ Gate ─ Executor ─ BinanceSpotAdapter`, `BinanceFeed`, `UserStream`을 그리지만, `BrokerAdapter`(`broker/base.py:58`) 구현체는 fake·kiwoom뿐이고(`place_order` 구현 grep: `broker/fake/*`, `broker/kiwoom/adapter.py:121`), `broker/binance/`에 WebSocket 연결 코드가 없다(`websockets` import는 `broker/kiwoom/realtime.py:132`뿐) | 설계 도식의 세 구성 요소가 코드에 없다 |
| P-4 | 엔진 판정·일 경계·대조 시점이 KST·KRX 달력 기준(§2 인벤토리 #3~#5) | M2 판정 시각(UTC, D-108)을 KST로 해석한다 |
| P-5 | m2-verify 초안 B-1~B-8 (지급 BTC 1, 실행 경로, capabilities, PAPER-0 예외, 승인, 수량 단위, 등록 경로, 시작 규칙 검사) | T-M2-15를 실제로 돌리기 전 해결할 것 — 이 CP가 B-2·B-3·B-4·B-6·B-7·B-8의 설계·작업을 정하고, B-1·B-5는 사용자 결정으로 남긴다 |

### 1.2 범위
- M2(바이낸스 Spot **테스트넷**) PAPER 실행 경로: 시작 점검 → 엔진 조립 → 판정(UTC) → 게이트 → 실행기 → 바이낸스 어댑터 → 사용자 스트림·대조·기록.
- 엔진 공통 코드의 시장 프로파일화(M1 동작 불변), M2 운영 도구 보강(등록·기준선·리셋 확인·운영 요청 주문 조건), M2 텔레그램 연결, 배포(M2 체크아웃).
- 오프라인 구현(T-045~T-049, fake·합성 응답·가짜 WS 서버, 네트워크 없음) → 테스트넷 관찰 run(T-050, **주문 차단**, 별도 승인) → T-M2-15(주문 전송, 별도 승인).

### 1.3 범위 밖
- 실전(LIVE) 도메인·키·주문 코드, Demo Mode 전환(A-062① — PAPER-1 전 재결정).
- M2 무인 자동 운영(CP-029의 M2판) — Q-2 결과에 따라 별도 CP.
- M2 매매전략(T-M2-17), M2 백테스트 판정 시각 복수화(m2-verify 오프라인 계획 4 — 별도 작업 제안).
- 테스트넷 지급 자산을 유니버스 보유에서 빼는 규칙(B-1 (나)) — 선택되면 별도 CP.
- `MarketDataStore`(KRX)·키움 경로 변경.

## 2. 현황 인벤토리 — M1 실행 경로가 하는 일 vs M2에 있는 것

"src 미사용" = 2026-10-15 grep 기준 `src/`에서 그 이름을 정의 파일 밖에서 부르는 곳이 없음(시험에서만 씀).

| # | 영역 | M1 실행 경로 (file:line) | M2에 있는 것 (file:line) | 빠진 것 / 문제 | 해소 |
|---|---|---|---|---|---|
| 1 | 진입·시작 검사 | `app/main.py:224-267` `run_main`: `check_startup`·배포 설정·슬롯·`check_start_scope(…, _m1_hosts)`·텔레그램 비밀 확인·`connection_approved` | `app/startup.py:106` `check_m2_startup`(접속 도메인 검사는 None — "연결 조립 때 검사"), `app/m2_preflight.py:146` `connect_m2_paper`(연결 조건 1~7, 테스트넷 호스트만) | M2 run 경로 자체, 연결 조립 때의 호스트 검사, 주문 전송 스위치 | D-202·D-204·D-209 / T-045·T-047 |
| 2 | 엔진 조립 | `app/wiring.py:71-159` `build_engine`: `BROKER_ENV`(47, PAPER=mock), `DailyRiskTracker`·`ConservativeCashFacts`(103-109), `StrategyRegistry(repo, clock, None)`(110, 시장 기본 M1), `GatePolicy.from_settings`(113 — market_time 기본 M1, `gate/ports.py:95-100`), `Executor`(114, asset_pair·client_ids 없음), `UnknownResolver`(120), `Reconciler`(121). 저장소는 기본 식별(`main.py:300`) | 시험 조립만: `tests/support/m2_fakes.py:180-223` (`Repository(order_identity=OrderIdentity.SYMBOL)`, `Executor(asset_pair, client_ids=True)`, `HaltManager(market_time=M2_TIME)`, `GateInputs(spot_rules=…)`, `DustHandler` + `executor.reject_listeners`), `scenario_dsl_m2.py:30-33` (`SpotUnknownResolver`, `SpotReconciler`) | 실행 코드의 M2 조립. `DustHandler`·`SpotUnknownResolver`·`SpotReconcileCycle` src 미사용 | D-201·D-202 / T-045 |
| 3 | 판정 스케줄러 | `app/engine.py:550-573` `_evaluate_due`: 오늘 = KST(551), KRX 세션(552-554), `datetime.combine(today, t, tzinfo=KST)`(557). `_next_eval`(576) KST, `_seed_evaluated`(230·235) KST, 판정 ID 시장 `"M1"` 고정(263), 스냅샷 시장 `"M1"`(610) | `domain/market_time.py` `M2_TIME`·`session_247`, `app/backtest_m2.py:70·222`(UTC 판정, 하루 1회) | 엔진이 M2 판정 시각을 KST로 해석, 판정 ID·스냅샷 시장 M1 고정 | D-201·D-207 / T-045 |
| 4 | 일 경계·일일 기준·보고 | `engine.py:404-453` `_daily`·`_report_if_due`: `last_kst_time_at_or_before`(408·446) | `monitoring/spot_cash.py:184` `SpotAccountFacts.start_day`, `monitoring/watchers.py:38`·`reporting/daily.py:26` `market_time` 인자(M2_TIME 지원) | 엔진 경로가 KST | D-201 / T-045 |
| 5 | 대조 시점 | `engine.py:482-516` 정규장 주기·마감 후 CLOSE(RV-25)·장 밖 K5 재시도(D-150) | `reconciliation/spot.py:57` `SpotReconciler`, `:360` `SpotReconcileSchedule`(시작·60초·스트림 재연결·UTC 일 경계), `:392` `SpotReconcileCycle`(`on_day_close`) | 엔진에 연결 안 됨(src 미사용) | D-201 / T-045 |
| 6 | 주문 유형·가격(의도 생성 시, 게이트 전) | `engine.py:627-630` 신호 → `GateRequest.from_signal` → `gate.decide` 직접 | `domain/trading_rules.py:174` `spot_order_terms`, `:244` `apply_spot_terms`(D-104·D-120). 실행 코드 호출은 `app/backtest_m2.py:265`뿐 | 엔진·운영 요청 경로에 M2 주문 조건 적용 없음 | D-201·D-211 / T-045·T-049 |
| 7 | dust | 해당 없음 | `execution/dust.py:66` `screen_exit`, `:80` `after_reject`, `:93` `review` | src 미사용 | D-201 / T-045 |
| 8 | 시장가 매도 수량 단위 (B-6) | 해당 없음(정수 주) | `trading_rules.py:201-209` `sellable_quantity`: MARKET_LOT_SIZE step 0이면 **내리지 않음**, G10(`:93`)도 step 0이면 단위 검사 없음 / `broker/binance/orders.py:82-83` 시장가 qty_step 0이면 "호출자가 LOT_SIZE로 대체", 배수가 아니면 `OrderBuildError`(`numbers.py:65-72`) / `broker/binance/rules.py:122-125` `market_qty_step()` = LOT_SIZE 대체 / `app/backtest_m2.py:159-162` LOT_SIZE로 내림. 테스트넷 실측 BTCUSDT MARKET_LOT_SIZE stepSize **0**, LOT_SIZE 0.00001 (binance-spot §12) | 경로마다 규칙이 다르다. 수수료 차감 순 수령(예 0.00015984)을 그대로 청산하면 어댑터가 전송 전 실패로 막아 **매일 청산 실패** | D-208 / T-046 |
| 9 | 브로커 어댑터 | `broker/kiwoom/adapter.py:121` `KiwoomAdapter`(BrokerAdapter), `main.py:388-436` `_paper_broker` | `broker/binance/client.py:120` `BinanceSpotClient`(1회 전송·4분류·가중치·-1021 감시), `readonly.py:51` `BinanceReadOnlyQueries`(조회 일부), `broker/rest_transport.py`(실제 HTTP, T-M2-14), `preflight.py:73` `PreflightGuard`(주문 계열 전송 전 차단) | `BrokerAdapter`+`SpotOrderQueries`(`base.py:40·58`) 구현체 없음 | D-203·D-204 / T-047 |
| 10 | 실시간 체결 통보 | `main.py:416-434` `KiwoomRealtime`·`order_handlers`·`ws_state_handler`(K5 실시간 경로) | `broker/binance/stream.py:101` `parse_event`, `:222` `FillLedger`, `:246` `SessionPolicy`(24시간 교체 시점 판정) — 파서·정책만 | WS API 연결(`userDataStream.subscribe.signature`)·재연결·스레드 없음 | D-205 / T-048 |
| 11 | 시세·규칙 공급 | `main.py:418·430-434` `KiwoomMarketFeed`·`MarketPoller` 스레드, `MarketDataStore`(KRX — `marketdata/store.py:35-58` KST 봉 확정) | `broker/binance/facts.py:89` `BinanceMarketFacts`(`update_ticker`·`update_klines`·`load_exchange_info`, `quote`·`rules`·`session`) | 폴링 스레드·호출 일정 없음. 쓰기(시세 스레드)·읽기(판정 스레드) 동시 접근 규칙 없음 | D-206·D-215 / T-048 |
| 12 | 시계 | `main.py:321-336` SNTP `ClockMonitor`(K6) | `client.py:226` `server_time_offset_ms`, `halt/broker_alerts.py` `BrokerAlertHalts`(418 → K5, -1021 연속 3회 → K6) — `:15` "저장소 동시 사용 방식은 M2 엔진 조립에서 정한다"(미결) | 10분 주기 서버 시각 확인(D-115) 일정, 정지 연결 방식 | D-206·D-215 / T-048 |
| 13 | 호출 제한 | 키움 `broker/ratelimit.py` | `weights.py:95` `WeightBudget`. 테스트넷 rateLimits 실측(§12: REQUEST_WEIGHT 6000/60초 → 내부 3000) — 적재 경로는 시작 점검에만 | run 시작 시 적재·예산 공유 | D-209 / T-047 |
| 14 | 결과 불명 해소 | `UnknownResolver`(wiring 120) | `execution/spot_resolver.py:35` `SpotUnknownResolver`(D-111) | src 미사용 | D-202 / T-045 |
| 15 | 현금·일일 리스크 | `AccountState` + `DailyRiskTracker` + `ConservativeCashFacts`(wiring 103-109) | `spot_cash.py:124` `SpotAccountFacts`(M2 현금 정책·UTC 일일 리스크, T-M2-07) — 실행 코드는 `m2_preflight.py:266`뿐 | 엔진 연결 | D-202 / T-045 |
| 16 | 기준선·리셋 확인 | 해당 없음 | `spot_cash.py:153` `set_baseline`, `reconciliation/spot.py:312` `confirm_reset`(활성 의도·열린 주문 있으면 거부, 유니버스 기초 자산 → 수동 보유, PAPER 기간 재시작 기록) / `m2_preflight.py:236-261` 기준선 없으면 `NO_BASELINE` | **사람이 실행할 도구 없음**(scripts에 없음). D-103 "최초 시작 기준선은 사람이 확정"의 수단이 없다 | D-210 / T-049 |
| 17 | 등록·상태 전이 (B-7) | `app/strategy_tool.py:65·67` `Repository(db, mode)`·`StrategyRegistry(repo, SystemClock(), None)` | `config/strategy_registry.py:245` `market` 인자 있음 | `scripts/strategy.py`에 `--market` 없음, 시장 기본 M1, DB 메타 검사·M2 식별(SYMBOL) 없음 | D-212 / T-049 |
| 18 | PAPER-0 검증 예외 (B-4) | `strategy_registry.py:47` `PAPER0_CAPABILITIES`(시장가·CAP-INS-01), `:78` markets M1만, `:80` S-4만 | — | M2판 예외 없음 | D-212 / T-049 |
| 19 | capabilities (B-3) | `docs/capabilities.md:12-35` M1 열 | M2 열 전부 "미지원", CAP-INS-05 행 없음(정의는 00 v0.5 §2.3 `00-strategy.md:117`, 개정안 `changes/CP-024-capabilities-draft.md:34`) → `strategy_registry.py:149-156` 등록 거부 | 표 반영 | D-212 / T-049 |
| 20 | 시작 시 전략 규칙 검사 (B-8) | `main.py:200-209` `validate_deploy`만 | m2-verify 초안 `M2Verify.validate_rules`(qty_step = LOT stepSize, notional ≥ 2 × minNotional) | 거래소 규칙 적재 뒤 전략 검사 호출 없음 | D-209 / T-046 |
| 21 | 운영 요청(/exit·/reduce) | `ops/commands.py:249-268` 운영 감액·청산 = 시장가 고정(지정가 거부) | T-M2-13 소수 수량(`telegram_run.py:42` market) | M2 지연 매도(D-120 "운영 요청 포함")·dust 선별 미적용 | D-211 / T-049 |
| 22 | 텔레그램 | `main.py:338-341` `telegram_threads(…, market=app.market)` | `config/m2/app.paper.toml` `[telegram] enabled = false`, 봇 토큰은 M2 체크아웃 비밀 설정(D-117) | M2 봇 생성·토큰 입력·연결 승인 여부 **미확인** | D-213 / Q-3 |
| 23 | 무인 운영 | `app/autopilot.py:1`("M1 PAPER 전용"), `:71` `KrxCalendar`, 예약 작업 06:45(M1 체크아웃) | 없음 | M2를 누가 언제 시작·정지하는지 | D-214 / Q-2 |
| 24 | 저장소·DB 메타 | `main.py:300-306` `bind_market(M1)` | `storage/market_meta.py` `check_db_file`(읽기 검사), M2 DB 아직 없음(§12 시작 대조 `NO_BASELINE`) | M2 DB 생성 때 메타 기록 경로 | D-202 / T-045 |
| 25 | 배포 체크아웃 | M1 = `C:\AI\trading_system\trading_system`(main) | `C:\AI\trading_system\m2-binance` 브랜치 `m2/run` = 6673a64 — main보다 36커밋 뒤(2026-10-15 `git log`), 작업 트리에 `config/m2/app.paper.toml` **로컬 수정**(사용자 접속 스위치, 커밋 안 됨) | 갱신 절차가 사용자 수정을 덮어쓰지 않아야 함 | D-216 |
| 26 | PAPER-0 시작 조건 | — | 04 2-M2.6 "M2 L1~L3 전부 통과" — V-157 미실행 4개(V-02·V-76 부분, V-112·V-127, `handoff.md:37`) | T-M2-09 잔여 | §8 차단 행 |
| 27 | 테스트넷 지급 BTC (B-1) | — | §12: 계좌 BTC 1·USDT 10,000·0이 아닌 자산 517개. 첫 대조에서 BTC 거래소 1 ↔ 내부 0 = `QTY_MISMATCH`(`reconciliation/spot.py:99`) → K4. 해소 `EXTERNAL`은 수동 보유(`ops/reconcile.py:4-5`), `confirm_reset`도 수동 보유 → 전략 진입 G6 "다른 소유자의 종목"(`gate/gate.py:200-202`). 기준 금액에 BTC 평가액이 들어가 L2 10%·L3 50% 초과 가능 | 사용자 결정 | Q-1 |

추가 관찰(이 CP에서 고치지 않음, §8에 기록): `trading_rules.py:174-180` `spot_order_terms`는 거래 규칙(`rules`)이나 시세가 없으면 **매도도** `None`(의도 없음)을 돌려준다. D-106 "규칙 미적재 시 위험 확대 거부, **위험 축소 허용**"과 다르다.

## 3. 결정 사항 (제안)

| ID | 결정 | 근거(R-ID·출처) | 대안과 기각 이유 |
|---|---|---|---|
| D-201 | **엔진은 하나, 시장 프로파일을 주입한다.** `Engine`에 시장 프로파일(MarketProfile)을 넣어 시장마다 다른 것만 바꾼다: (1) 시장 시각 `M1_TIME`/`M2_TIME` — 판정 시각·`_seed_evaluated`·`_next_eval`·일일 기준·보고 시각을 이 시각대로 해석 (2) 세션 공급 — M1 `KrxCalendar`, M2 `session_247` (3) 대조 일정 — M1 현 `_reconcile_if_due` 그대로, M2 `SpotReconcileCycle`(시작·60초·스트림 재연결·UTC 일 경계, `on_day_close` → 일일 보고) (4) 판정 입력 구성 — M1 `MarketDataStore.snapshot`, M2 D-207 (5) **게이트 전 요청 변환 훅** — M1 항등, M2 `apply_spot_terms`(None이면 의도를 만들지 않고 감사 기록·알림) → 청산·감액이면 `DustHandler.screen_exit` (6) 계좌 사실 — M1 `AccountState`(현 경로), M2 `SpotAccountFacts` (7) 판정 ID·스냅샷의 시장 코드. M1 프로파일은 현재 객체로 만들어 **M1 동작을 바꾸지 않는다**(V-152). 엔진 본문에 `if market == …` 분기를 흩뿌리지 않는다 | R-04·R-14·R-26, 03 D-038·D-092, CP-024 D-108(대안 "M2 전용 엔진: 주문 경로 이원화"를 이미 기각)·D-109·D-120 | (가) M2 전용 엔진 클래스(복사): 주문 경로가 둘이 되어 정지·결과 불명·재시작 규칙을 두 번 유지해야 함 — D-108에서 기각된 형태. (나) 엔진 상속: 판정·대조 단계가 메서드 안에서 KST를 직접 써서 결국 같은 수정이 필요하고 재정의 누락 위험 |
| D-202 | **조립 지점은 `app/wiring.build_engine` 하나.** `market` 인자로 M2 구성 요소를 고른다: `Repository(order_identity=SYMBOL)`(D-110), `bind_market(M2, binance-spot-testnet, created=…)`, `Executor(asset_pair, client_ids=True)`, `SpotUnknownResolver`, `SpotReconciler(universe, quote_asset, expected_quote)`, `HaltManager(market_time=M2_TIME)`, `GatePolicy(market_time=M2_TIME, cost_rates = config/m2/market + quoteAssetPrecision)`, `GateInputs(spot_rules=BinanceMarketFacts)`, `DustHandler` + `executor.reject_listeners`, `AlertWatcher`·`DailyReporter`(M2_TIME), `StrategyRegistry(market="M2")`, CP-035 시작 검사(배포에서 빠진 전략 보유) 그대로. `app/main.run_main`의 M2 분기는 M1 `_run`과 같은 골격(실행 잠금·STOP 파일·감시 스레드·텔레그램·종료 순서)을 쓴다. 시험 조립(`tests/support/m2_fakes.py`)도 이 함수를 쓰도록 옮겨 실운영·시험 구성을 하나로 한다(03 D-093 "실운영과 시험이 같은 함수") | 03 D-093, CP-024 D-097·D-110·D-111·D-118, R-01·R-05·R-06 | 시험 조립 유지 + 실행용 별도 조립 함수: 시험이 실운영 조립을 검증하지 못함 |
| D-203 | **바이낸스 브로커 어댑터** `BinanceSpotAdapter`(`broker/binance/` 안, D-119)가 `BrokerAdapter`·`SpotOrderQueries` 계약을 구현한다. `BinanceSpotClient` 하나를 감싼다. `place_order`: 매수 지정가 → IOC 지정가, 매도 시장가 → 수량 지정 시장가, 매도 지정가(시세 지연) → IOC 지정가, 시장가 매수는 전송 전 거부(D-104). 고객 주문 ID = 멱등키에서 결정적 생성(D-110). **1회 전송, 자동 재전송 없음**, 결과는 4분류(D-112) 그대로 `SubmitResult`로. FULL 응답 체결은 체결 ID로 사용자 스트림과 중복 제거(`FillLedger`) 후 이벤트 큐로. `modify_order` = 전송 전 거부(정정 미지원). `cancel_order` = 1회 전송. `query_cash`·`query_balances`·`query_orders`·`query_fills`·`lookup_order`·`orders_between`·`trades_between`은 `BinanceReadOnlyQueries` 매핑을 재사용. 경보(418·-1021)는 `on_alert` → D-215 | CP-024 D-104·D-110~D-113·D-118·D-119, 03 §5.1·2-M2.5, R-05·R-06·R-09·R-16 | 클라이언트를 엔진이 직접 호출: 03 §7 의존 규칙(엔진은 공통 계약만) 위반 |
| D-204 | **테스트넷 주문 전송 스위치를 접속 스위치와 따로 둔다.** `config/m2/app.paper.toml [paper]`에 `orders_approved`(키 없으면 false)와 `orders_approval`("A-숫자")을 둔다. 둘 다 사용자가 직접 쓴다 — 에이전트는 바꾸지 않는다. (1) `connection_approved = true`만이면 **주문 차단 run**: 전송 계층이 주문 계열 경로(`preflight.is_order_family`와 같은 분류)를 전송 전에 막는다(전송 0, 의도는 전송 전 실패로 종결·알림). 이 상태에서 전략을 배포하면 시작 거부. (2) `orders_approved = true` + 승인 A-ID 형식 맞음일 때만 주문 계열 허용, 시작 출력·감사에 A-ID 기록. (3) 접속 대상은 시작 일치 표의 테스트넷 호스트만(`config/market_scope.py:29-30`), LIVE는 기존대로 시작 거부 | CLAUDE.md §3·§7, A-096(조회만), CP-024 D-098, R-01 | (가) `connection_approved` 재사용: 이 값은 A-096(조회만) 때문에 사용자 작업 트리에서 이미 켜져 있어, 병합 즉시 주문이 열린다 — 위험. (나) 코드 상수로만 막기: 사용자가 승인 시점에 직접 켤 수단이 없음 |
| D-205 | **사용자 스트림 = WS API `userDataStream.subscribe.signature`**(D-114 그대로), 전용 스레드 하나. 서버 ping에 pong, `serverShutdown`·끊김·24시간 전 교체(`SessionPolicy`) → 재연결(03 D-050과 같은 계속 시도) → `STREAM_RECONNECT` 대조(D-109). 끊김 = K5 실시간 경로(M1 `ws_state_handler`와 같은 규칙), 재연결 뒤 대조 성공이 해제 조건 보고. 체결 통보는 어댑터 이벤트 큐에만 넣고 반영은 판정·실행 스레드(`executor.process_events`)가 한다. 구독 거부(서명·키 권한)는 K5 + 긴급 알림 — PV-M2-06 불통과 시 키 유형 재결정(A-063⑨) | CP-024 D-109·D-114, 03 D-038·D-050, R-08·R-09, V-150 | 체결 조회 폴링만: PV-M2-05·06을 검증할 수 없고 승인된 D-109·D-114(스트림 재연결 후 대조)를 바꾸는 것이라 별도 CP 필요 |
| D-206 | **시세·규칙·서버 시각 공급 스레드**(`binance-feed`) 하나가 `BinanceMarketFacts`를 갱신한다: 유니버스 최근가 주기 조회(제안 5초 — 신선도 60초의 1/12), 보유가 있을 때 avgPrice·referencePrice(제안 60초, dust 판정 D-102), 1d klines(시작·UTC 00:00 뒤, 진행 중 봉 미사용), exchangeInfo(시작·UTC 00:00 뒤·-1013 명시 거부 뒤, D-106·2-M2.3), 서버 시각(10분, D-115 — 0.5초 이상 경고). 호출은 우선순위 시세(최하위, D-113)로 같은 `WeightBudget`을 쓴다. 데이터 품질 실패 → K10(큐 경유). 주기 값은 [제안] — 엔드포인트 weight는 구현 착수 때 BN §8 원문으로 다시 확인해 기록 | CP-024 D-102·D-106·D-113·D-115·D-116, R-09·R-22·R-24 | 판정 시각에만 조회: 시세 신선도(G11)·지연 매도 판정(D-120)·dust 참조가에 쓸 값이 없음 |
| D-207 | **M2 판정 입력**: 유니버스 심볼마다 확정 1d 봉(`daily_bar_is_final`), 최근가, 심볼 상태, 보유 표시. 보유 표시 수량 = **청산 가능 수량**(D-208), 0·minQty 미만·확실한 dust면 보유 없음(D-102). `owned_by_me` = 소유권 소유자 비교(수동 보유는 False). `lots` = LOT_SIZE stepSize. 판정 시각은 UTC(D-108), 판정 ID 시장 코드 `M2`, 재판정 방지(T-044)도 UTC 날짜 기준 | CP-024 D-102·D-108, 02 판정 스냅샷, m2-verify 초안 "판정 입력 요구", R-14·R-26 | `MarketDataStore` 재사용: KRX 달력·KST 봉 확정(`store.py:35-58`)이 섞임 |
| D-208 | **시장가 매도 수량 단위(B-6)**: MARKET_LOT_SIZE stepSize가 0이면 시장가 매도 수량도 **LOT_SIZE stepSize로 내림**한다. 판정 입력(D-207)·`sellable_quantity`·`dust_verdict`·G10·어댑터 주문 문자열·M2 백테스트가 같은 규칙(`broker/binance/rules.py:122` `market_qty_step()`과 같음)을 쓴다. 내림 잔량은 dust(D-102, 원장·대조 유지, 자동 정리 없음). step 0의 거래소 의미는 PV-M2-08 실측 뒤 다르면 CP로 바꾼다 | CP-024 D-102·D-106·D-118, BN §12 실측(MARKET_LOT_SIZE step 0), m2-verify B-6, R-16·R-28 | (가) 내리지 않고 보냄(현 `trading_rules.py:205-209`): 어댑터가 배수 아님으로 전송 전 실패(`orders.py:73-79`) → 매일 청산 실패, 거래소 수용 여부도 미확인. (나) 두 step의 공배수: step 0의 의미가 미확인이라 근거 없음 |
| D-209 | **M2 시작 순서**: TRADE_MODE·`--market M2` → `check_m2_startup` → `connection_approved`·비밀 키 존재(값은 읽어 넘기기만)·호스트 검사 → 주문 스위치(D-204) → 실행 잠금 → 바이낸스 서버 시각(0.5초 경고) → exchangeInfo 적재(규칙·rateLimits → 예산 50%; 실패하면 **시작 거부**, D-113) → 전략 `validate_deploy`·`validate_rules`(있으면; 거래소 규칙과 불일치면 시작 거부, B-8) → 등록(M2 등록부) → `engine.start()`(T-044 판정 기록 복원 → 결과 불명 재시작 정리 → **K8** → 잔고 조회 → 시작 대조) → 사용자 스트림·시세 스레드 시작 → **K8은 사용자 재개로만**. 브로커 주문은 시작 대조 뒤 사용자 재개 전에는 없다 | CLAUDE.md §8-9, 01 §2.11, CP-024 D-106·D-113·D-115, R-01·R-24 | 시작 점검 도구(binance-preflight)를 run 전에 따로 실행하게 함: 점검과 실행 사이에 상태가 바뀔 수 있고 사용자 단계가 늘어남(도구는 그대로 둔다) |
| D-210 | **기준선 확정·리셋 확인 운영 도구**: 기존 로컬 도구 `scripts/reconcile-resolve.py`에 M2 하위 명령 2개 — `initial-baseline`(D-103 "최초 시작 기준선은 사람이 확정") / `reset-confirm`(D-099). 공통 규칙은 `confirm_reset`(`reconciliation/spot.py:312`): 활성 의도·열린 주문 있으면 거부, USDT 기준선 = 거래소 free + locked, 유니버스 기초 자산 = 수동 보유(manual), 범위 밖 자산 스냅샷, 감사 기록(최초는 PAPER 기간 시작, 리셋은 재시작 표시). **엔진 정지 중(실행 잠금 없음)에만, 로컬만**(원격 명령 없음). 연결은 조회만(A-096 범위). K4는 풀지 않는다 — 다음 대조 일치 + 사용자 재개 | CP-024 D-099·D-103, A-062①⑤, R-01·R-08 | (가) 첫 대조 불일치를 `EXTERNAL`로 하나씩 해소: USDT 기준선을 정할 수단이 없음. (나) 자동 기준선: D-099에서 기각(외부 거래와 구분 근거 없음) |
| D-211 | **운영 요청(/exit·/reduce)도 M2 주문 조건을 탄다**: `OpsProcessor`가 D-201 (5) 훅을 거쳐 게이트로 보낸다 — 시세 정상 = 시장가, 지연 = IOC 지정가(D-120 "전략 신호·운영 요청 모두"), 청산이면 dust 선별. 수량은 LOT_SIZE 단위 배수가 아니면 거부(자동 반올림 없음 — 사람이 정한 수량을 바꾸지 않음). 소유자 무관 위험 축소 허용(G6, 01 §2.5)은 그대로 | CP-024 D-102·D-117·D-120, A-065 Q4·A-066, R-13·R-21 | 운영 요청은 시장가 고정 유지: 시세 지연 중 /exit가 G11에서 거부되어 위험 축소가 막힘 |
| D-212 | **M2 등록·상태 전이·PAPER-0 예외(B-3·B-4·B-7)**: (1) `scripts/strategy.py`에 `--market M1\|M2`(기본 M1) — DB 메타 검사(`bind_market`, 다른 시장·메타 없는 DB에 M2면 아무것도 쓰지 않고 거부), M2는 SYMBOL 식별, `StrategyRegistry(market="M2")` (2) PAPER-0 검증 예외의 M2판 — markets `("M2",)`, sizing S-1 또는 S-4, 능력 ⊆ {CAP-TRG-01, CAP-DAT-01, CAP-ORD-01, CAP-ORD-02, CAP-INS-05}, `l7_max_order_amount` 필수, 사용자 승인 A-ID 필수. M1 예외(`strategy_registry.py:47·71-90`)는 그대로 (3) capabilities.md: CAP-INS-05 행 추가, M2 열 = CAP-TRG-01·CAP-DAT-01·CAP-ORD-01(시장가 **매도만**)·CAP-ORD-02(IOC 지정가 매수·지연 중 IOC 매도, 가격은 시스템 규칙)·CAP-INS-05 "검증 중", CAP-ORD-03a 미지원(정정 없음), 03b 미지원(전략 실행 정책 취소 없음 — 운영·정지 취소만). "검증 중" 표기는 T-045~T-049 L1·L2 통과 뒤, 문서 승인 뒤, M1 run이 꺼진 시간에 교체(M1 코드가 이 파일을 읽음) | 00 D-004·§2.3 CAP-INS-05, CP-024 §3 capabilities 행(A-066 방향 승인), DP-24 a(A-037), R-17·R-20 | M1 예외를 시장 무관으로 넓힘: M1 PAPER-0 규칙(S-4·시장가만)이 함께 바뀜 — 시장별로 분리 |
| D-213 | **M2 텔레그램 = M2 전용 봇**(D-117, A-063⑧). 토큰·대화방은 M2 체크아웃 비밀 설정에만, 메시지 앞 `[M2]`, 명령 범위는 M1과 같은 규칙(CP-026: `/resume K8`·`/resume K4` 원격 조건, `/kill`, `/exit` 소수 수량)이며 M2 DB에만 작용. 봇 생성·토큰 입력·`[telegram] enabled = true`는 사용자가 직접, 실제 송수신은 텔레그램 연결 승인(T-M2-13) 뒤 | CP-024 D-117, A-063⑧, 03 D-052, R-13·R-21 | M1 봇 공유: 명령이 어느 시장 DB에 가는지 섞임(D-117 기각 사유) |
| D-214 | **[Q-2 결정 대기] 실행 방식 = PAPER-0 동안 수동 run**(권장): 사용자가 출발 전(KST 07:15 전)에 M2 체크아웃에서 `run --market M2`를 시작하고 그대로 둔다(24/7 시장 — 저녁에 종료하거나 계속 실행). 프로세스가 죽거나 PC가 재시작되면 그 사이 판정은 없다(주문 0). 다시 시작하면 K8 → 시작 대조 → 텔레그램 `/resume K8`(CP-026 조건 충족 시)로 재개. M2 무인 자동 운영(예약 작업·재시작 감시)은 PAPER-0 첫 주 관찰 뒤 별도 CP | 사용자 운영 조건(평일 07:15~저녁 부재, 텔레그램만), CP-029(M1 전용·KRX 달력), A-098(재부팅 대책), CLAUDE.md §4(자동 실행 등록 금지 — 승인 필요) | 지금 CP-029를 M2로 확장: 24/7 일정·일 경계·재부팅 처리가 M1과 달라 설계가 커지고 PAPER-0 시작이 늦어짐 |
| D-215 | **스레드·저장소·정지 연결**: M1 D-038과 같다 — 주문은 판정·실행 스레드만. 사용자 스트림·시세·텔레그램·감시 스레드는 큐(`call_soon`)와 잠금 저장소에만 쓴다. 어댑터 경보(418 → K5, -1021 연속 3회 → K6)는 `BrokerAlertHalts`를 `QueuedHalts` 경유로 연결(발동이 주문을 내지 않는 유형이어도 저장 순서를 판정·실행 스레드로 모음 — `broker_alerts.py:15` 미결 해소). `BinanceMarketFacts`는 잠금으로 보호하고 판정은 한 시점의 복사본을 읽는다. 감시 스레드는 `binance-feed`(죽으면 긴급 알림 + K5)와 사용자 스트림 스레드(죽으면 K5 실시간 경로)를 본다 | 03 D-038·RV-22, CP-024 D-113·D-115, R-09 | 어댑터 스레드에서 정지 직접 발동: 저장소 순서가 스레드마다 달라 시험 재현이 어려움 |
| D-216 | **배포**: M2는 `C:\AI\trading_system\m2-binance`(브랜치 `m2/run`)에서만 실행한다(D-096). `m2/run` 갱신은 사용자 지시 때(A-097⑤ 방식). 사용자 로컬 수정(`config/m2/app.paper.toml` 접속 스위치)을 덮어쓰지 않도록 **이 CP의 코드는 추적 중인 `config/m2/app.paper.toml`을 바꾸지 않는다** — 새 키(`orders_approved`·`orders_approval`)는 없으면 false로 읽고, 사용자가 승인 때 직접 추가한다. `main` 병합은 M1 실행본(main 체크아웃)을 바꾸므로 M1 전체 회귀 통과 뒤, M1 자동 운영이 끝난 시간(15:45 이후)에 사용자 지시 시 | CP-024 D-096, 03 2-M2.1, A-097⑤, D-082·D-085 | 설정 파일에 새 키를 기본 false로 추가: 사용자 작업 트리 수정과 병합 충돌 |

## 4. 작업 (05-plan에 추가 제안)

공통: 작업마다 브랜치 `feat/T-0xx-<요약>`(D-085), 시험 먼저 작성 → 실패 확인 → 최소 구현 → 관련·M1 전체 회귀(V-152) → 로컬 커밋(D-082). **T-045~T-049는 네트워크 없음**(fake·합성 응답·가짜 WS 서버). **테스트넷 연결은 T-050 별도 승인 뒤, 테스트넷 주문 전송은 T-M2-15 승인으로만 한다.** 실전 주문 코드·LIVE는 어느 작업에도 없다.

| T | 이름 | 연결 R / D | 선행 | 산출물 | 정상 검증 | 오류 검증 | 완료 기준 | 권한·데이터·확인 |
|---|---|---|---|---|---|---|---|---|
| T-045 | [CP-036] **M2 최소 종단 간 run (fake)** | R-03·R-04·R-05·R-07·R-08·R-14·R-26 / D-201·D-202·D-207(최소)·D-215 | 이 CP 승인 + 구현 착수 지시 | 엔진 시장 프로파일(M1 프로파일 = 현 동작), `build_engine(market=M2)`, `run --market M2`의 BACKTEST 경로(`FakeSpotBroker` + 가상 시세 공급), M2 판정 스케줄(UTC)·`SpotReconcileCycle`·`apply_spot_terms`·dust 훅·`SpotUnknownResolver`·`SpotAccountFacts` 연결, M2 DB 메타 기록, 시험 조립(`m2_fakes`)을 `build_engine`으로 이전, 시험 전용 고정 전략(가상 심볼) | V-280, V-281(신호 경로), V-283(f): 가상 신호(UTC 01:00) → 게이트 → IOC 지정가 매수 체결(수수료 = BTC) → 순 수령 포지션·소유권 → UTC 04:00 시장가 매도 → dust → 소유권 해제 → 다음 날 재진입 | 주문 차단 5종(KILL 파일, K1, 한도 초과, 중복 신호, 저장 실패) 각각 브로커 전송 0, V-283(e) 재시작 → K8 → 대조 전 주문 0 | V 통과, INV 위반 0, **V-152 M1 전체 회귀 통과(건수 보고)**, 시험 조립과 실운영 조립이 같은 함수 | 가상 데이터만(가상 심볼 `TSTA-USDT`·가상 계좌 `BN-BT-01`) |
| T-046 | [CP-036] 수량 단위·M2 판정 입력·시작 규칙 검사 | R-14·R-16·R-28 / D-207·D-208·D-209 | T-045 | `sellable_quantity`·`dust_verdict`·G10·어댑터 수량 규칙 통일(step 0 → LOT_SIZE 내림), M2 판정 입력 구성기, 전략 선택 메서드 `validate_rules` 시작 호출, M2 백테스트 같은 규칙 확인 | V-282, V-283(a)~(d) | 순 수령 0.00015984 → 0.00015 매도, 잔량 dust, 규칙 불일치·exchangeInfo 실패 시작 거부(주문 0) | V 통과, 기존 V-131·V-160 시험 기대값 변경이 있으면 사유(D-208) 기록, V-152 | 가상 데이터 + §12 실측 규칙값(공개 규칙값, 민감정보 아님) |
| T-047 | [CP-036] 바이낸스 어댑터·주문 전송 스위치 | R-05·R-06·R-09·R-11·R-12·R-16 / D-203·D-204·D-209(예산 적재) | T-045 (T-046과 병행 가능) | `broker/binance/` 어댑터(BrokerAdapter·SpotOrderQueries), 주문 차단 전송 계층(`orders_approved`), 시작 시 rateLimits 예산 적재, 비밀 값 마스킹 확인 | V-284, V-285(b)(d) | V-285(a)(c)(e), 429·5XX·타임아웃 → 결과 불명·재전송 0, 정정 → 전송 전 거부 | 합성 응답 계약 시험 통과, **실전·Demo 도메인 상수 없음 확인**, 로그·오류 문구에 키·서명·쿼리 문자열 없음, V-152 | 네트워크 없음. 합성 응답은 공식 문서 필드 정의로(D-084 방식) |
| T-048 | [CP-036] 사용자 스트림 WS·시세·규칙·서버 시각 공급 | R-08·R-09·R-22·R-24 / D-205·D-206·D-215 | T-047 | WS API 구독 연결 관리(ping/pong·serverShutdown·24시간 전 교체·재연결), 체결 통보 → 어댑터 큐, `binance-feed` 스레드, `BinanceMarketFacts` 잠금, 감시 스레드 감시 대상 추가, `BrokerAlertHalts` 큐 연결 | V-286, V-287 | 끊김 → K5 → 재연결 → 대조, 구독 거부 → K5·긴급 알림, 시세 지연 → G11, 예산 부족 → 시세 보류 | V 통과, 체결 반영이 판정·실행 스레드에서만 일어남을 시험으로 확인, V-152 | 네트워크 없음(가짜 WS 서버·합성 응답). 기존 의존성 `websockets` 재사용 — 현 고정 버전으로 WS API 서명 구독이 되는지 착수 때 확인, 새 설치가 필요하면 05 §7 절차로 **먼저 승인** |
| T-049 | [CP-036] M2 운영 도구·등록·텔레그램 연결 | R-01·R-08·R-13·R-17·R-20·R-21 / D-210·D-211·D-212·D-213 | T-045 | `reconcile-resolve` M2 하위 명령(`initial-baseline`·`reset-confirm`), `strategy.py --market`, PAPER-0 예외 M2판, 운영 요청 M2 주문 조건, `run --market M2`의 텔레그램 연결(가짜 서버), capabilities.md M2 열·CAP-INS-05 **개정안**(반영은 문서 승인 뒤) | V-288, V-289, V-290 | 활성 의도 중 기준선 거부, 원격 기준선 거부, 다른 시장 DB 거부, 예외 범위 밖 거부 | V 통과, M1 PAPER-0 예외 시험 결과 불변, V-152 | 네트워크 없음. capabilities.md 교체는 사용자 승인 + M1 run 꺼진 시간 |
| T-050 | [CP-036] **M2 테스트넷 관찰 run (주문 차단)** — 사용자 실행 | R-01·R-08·R-09·R-24 / D-204·D-205·D-206·D-209·D-210 | T-045~T-049 main 병합 → `m2/run` 갱신(사용자 지시), **테스트넷 연결 범위 승인(Q-5)**, M2 텔레그램(Q-3) | 관찰 기록(07-paper-review M2 절 또는 binance-spot §12 다음 절): 기준선 확정 → 시작 순서 → K8 재개 → 사용자 스트림 구독(PV-M2-06 겸) → 60초 대조 연속 MATCH → (가능하면) UTC 00:00 통과·24시간 스트림 교체 | PV-M2-12 | 끊김·재연결 시 대조, 주문 계열 전송 0(전송 계층 차단 기록) | 결과 기록(미실행 항목 사유 포함), **주문 0건** | 사용자가 명령 직접 실행(05 D-086 방식). 전략 배포 없음. 지급 BTC는 이 단계에서 수동 보유로만 기록(매도 없음) |
| (기존) T-M2-15 | M2 PAPER-0 | 04 2-M2.6 / D-099·D-105 / PV-M2-01~11 | **T-050 결과**, V-157 잔여 4개(§8), M2 검증 전략 승인, **테스트넷 주문 전송 승인**, Q-1 처리 | 사용자가 `orders_approved`·`orders_approval`을 직접 설정하고 run 실행 | (기존 행) | (기존 행) | (기존 행) | 주문 실행은 사용자 |

순서: T-045 → (T-046 ∥ T-047) → T-048 → T-049 → [병합·체크아웃 갱신] → T-050 → T-M2-15. 기한·공수는 정하지 않는다(미확인).

## 5. 새 검증 항목 (04 2-M2.3에 추가 제안)

| V-ID | 항목 | 입력 | 기대 결과 | 등급 | 근거 |
|---|---|---|---|---|---|
| V-280 | M2 판정 시각·일 경계 [CP-036] | fake run, 판정 시각 UTC 01:00·04:00, 시계 UTC 00:59 → 04:06, 토·일 날짜, KST 00:00 통과, 01:03 재시작, 01:06 첫 tick | UTC 01:00·04:00에만 판정(KST 해석 아님), 판정 ID 시장 M2, 주말에도 판정, 유예 5분 초과는 건너뜀 기록, 재시작 뒤 같은 판정 시각 재판정 없음(T-044 M2판), L4·L5·기준 금액·보고는 UTC 00:00 경계, KST 자정은 영향 없음. 같은 입력의 M1 프로파일 결과는 변경 전과 같음 | L2 | D-201·D-207, CP-024 D-108, A-055 |
| V-281 | 의도 생성 시 M2 주문 조건 [CP-036] | (a) 진입 신호, 시세 정상 (b) 청산 신호, 시세 정상 (c) 청산 신호, 시세 61초 지연 (d) 진입 신호, 시세 지연 (e) 거래 규칙 미적재 + 진입 (f) 확실한 dust의 청산 신호 | (a) IOC 지정가, 가격 = 최근가 × 1.005 tick 내림, 예약 = 주문가 (b) 수량 지정 시장가 (c) IOC 지정가 매도, 마지막 정상 시세 × 0.995 tick 올림 (d) 의도 생성 뒤 G11 거부 (e) 의도 없음 + 감사·알림 (f) 전송 없음, DUST_CONFIRMED, 소유권 해제. 모든 경우 게이트는 요청을 바꾸지 않음 | L2 | D-201·D-211, CP-024 D-102·D-104·D-120 |
| V-282 | 시장가 매도 수량 단위 [CP-036, B-6] | §12 규칙값(LOT_SIZE step 0.00001, MARKET_LOT_SIZE step 0, minNotional 5), 순 수령 0.00015984 BTC, 시세 정상·지연 / 가상 규칙 MARKET_LOT_SIZE step 0.0001 | 판정 입력 보유 = 0.00015, 청산 신호 0.00015(판정 실행기 버림 없음), G10 통과, 어댑터 주문 문자열 오류 없음, 체결 뒤 잔량 0.00000984 = dust → 소유권 해제 → 다음 날 진입 허용 / 가상 규칙은 0.0001 단위 내림 | L1·L2 | D-208, CP-024 D-102·D-118 |
| V-283 | M2 시작 순서·재시작 [CP-036] | (a) exchangeInfo 실패 (b) rateLimits 없음 (c) `validate_rules` 불일치(qty_step ≠ LOT step 또는 notional < 2 × minNotional) (d) 등록 거부 전략 (e) 결과 불명 의도가 남은 채 재시작 (f) 정상 | (a)~(d) 시작 거부, 종료 코드 2, 브로커 주문 0 (e) K8, 해소 조회만, 같은 의도 재전송 0, 대조 + 사용자 재개 전 주문 0 (f) K8 → 시작 대조 → 사용자 재개 뒤에만 판정·주문 | L2 | D-209, CLAUDE.md §8-6·§8-9, CP-024 D-111·D-113 |
| V-284 | 바이낸스 어댑터 계약 [CP-036] | 합성 응답: FULL 전체 체결, IOC 부분 체결, IOC 체결 0 EXPIRED, -1013, -2010 비중복·중복 메시지, 429, 5XX, 전송 후 타임아웃, 정정 요청, 재시작 후 같은 의도 | 체결 이벤트·EXPIRED 예약 해제·명시 거부·결과 불명이 D-112 표대로, 모든 경우 전송 1회·재전송 0, 고객 주문 ID 결정적(재시작 후 같음), 정정 = 전송 전 거부, FULL 체결과 스트림 체결 중복 1회 반영 | L1 | D-203, CP-024 D-110·D-112·D-113 |
| V-285 | 주문 전송 스위치 [CP-036] | (a) `orders_approved` 없음·false + 전략 배포 (b) false + 전략 없음 + 운영 `/exit` (c) true + `orders_approval` 없음·형식 오류 (d) true + A-ID (e) 접속 대상이 시작 일치 표 밖(실전·Demo 호스트) | (a) 시작 거부 (b) 시작, 주문 계열 경로 전송 전 차단(전송 0), 의도 전송 전 실패 종결·알림 (c) 시작 거부 (d) 주문 경로 허용, 시작 출력·감사에 A-ID (e) 시작 거부 | L1·L2 | D-204, CP-024 D-098, A-096 |
| V-286 | 사용자 스트림 [CP-036] | 가짜 WS 서버: 구독 성공, executionReport(부분·전체·EXPIRED·TRADE_PREVENTION), FULL 체결과 같은 체결 ID, 끊김, serverShutdown, 23시간 50분 경과, 구독 거부(서명 오류) | 반영은 판정·실행 스레드에서만, 같은 체결 1회, TRADE_PREVENTION은 체결 아님, 끊김 → K5(실시간 경로) + 재연결 → STREAM_RECONNECT 대조 → 해제 조건 보고, 24시간 전 교체, 구독 거부 → K5·긴급 알림 | L2 | D-205·D-215, CP-024 D-109·D-114, V-150 |
| V-287 | 시세·규칙·서버 시각 공급 [CP-036] | 합성 응답: 최근가 주기, 응답 지연(61초 공백), 진행 중 봉 포함 1d klines, UTC 00:00 통과, -1013 수신, 서버 시각 오차 0.6초, 예산 부족 | 시세 시각 = 수신 시각, 61초 공백 → 위험 확대 거부·지연 매도 규칙, 진행 중 봉 미사용, 일 경계 exchangeInfo·klines 재적재, -1013 뒤 규칙 재적재, 0.5초 이상 경고, 예산 부족 시 시세 호출이 먼저 보류 | L2 | D-206, CP-024 D-106·D-113·D-115·D-116 |
| V-288 | 기준선 확정·리셋 확인 [CP-036] | (a) 새 M2 DB, 가상 거래소 잔고 BTC 1·USDT 10,000·범위 밖 자산 (b) 활성 의도·열린 주문 있음 (c) 텔레그램(원격)에서 요청 (d) 엔진 실행 중(잠금) (e) 리셋 모의(USDT 재지급·주문 소실) | (a) USDT 기준선 = free + locked, BTC 1 = 수동 보유, 범위 밖 자산 스냅샷, 감사(PAPER 기간 시작), 다음 대조 MATCH, 이후 전략 진입은 G6 거부(B-1 재현) (b)(c)(d) 거부, 아무것도 쓰지 않음 (e) K4 → `reset-confirm` → PAPER 기간 재시작 기록, K4는 대조 일치 + 사용자 재개로만 해제 | L2 | D-210, CP-024 D-099·D-103 |
| V-289 | M2 전략 등록·상태 전이 [CP-036] | (a) `strategy.py --market M2` + M2 DB (b) `--market M2` + M1 DB·메타 없는 DB (c) PAPER-0 M2 예외 범위 안(markets M2, S-1, 허용 능력, l7, A-ID) (d) 범위 밖(CAP-ORD-03b, markets M1+M2, l7 없음, A-ID 없음) (e) 기존 M1 예외 시험 | (a) 시장 M2 행 등록·전이 (b) 거부, 쓰기 없음 (c) 등록 통과 (d) 거부 사유 각각 (e) 결과 불변 | L1 | D-212 |
| V-290 | 운영 요청 M2 [CP-036] | 텔레그램 `/exit BTCUSDT 1`(수동 보유), 시세 정상·지연, K1 중, LOT 단위 배수 아닌 수량 | 소유자 무관 허용(G6), 정상 = 시장가, 지연 = IOC 지정가, K1 중에도 위험 축소 허용, 배수 아님 = 거부(자동 반올림 없음) | L2 | D-211, CP-024 D-117·D-120 |

| PV | 확인 | B-U | 결과 반영 |
|---|---|---|---|
| PV-M2-12 [CP-036] | 테스트넷 관찰 run(주문 차단, T-050): 시작 순서·K8 재개·사용자 스트림 구독·60초 대조 연속 MATCH·시세 호출량·(가능하면) 일 경계·스트림 교체, 주문 계열 전송 0 | B-U8·B-U9(PV-M2-05·06 일부 겸) | 결과 기록, T-M2-15 시작 판단 입력 |

## 6. 결정 필요 (최대 5개)

| Q | 질문 | 선택지 | 권장 | 차단 |
|---|---|---|---|---|
| Q-1 | **B-1 테스트넷 지급 BTC 1개 처리** — 기준선 확정(D-210) 때 수동 보유로 기록되어 전략 진입이 G6 "다른 소유자의 종목"으로 거부되고(`gate.py:200-202`), L2 10%·L3 50%도 넘을 수 있다 | (가) T-M2-15 시작 직후 **운영 요청 `/exit`로 지급 BTC 전량 매도**(위험 축소, 소유자 무관 허용 — 테스트넷 주문 1건이므로 T-M2-15 주문 전송 승인 범위에 넣는다). 리셋(약 월 1회, BN §2.1) 뒤마다 반복 / (나) 지급 자산을 유니버스 보유에서 빼는 규칙 — 별도 CP(대조·L2·L3·G6에 "지급 보유" 구분이 필요하고, 외부 거래와 구분 근거가 없어 D-099 취지와 충돌) / (다) 지급이 없는 심볼로 유니버스 변경 — 517개 자산이 지급되어 가능성 낮음, A-096(BTCUSDT) 재결정 | (가) | **차단**(T-M2-15) |
| Q-2 | **M2 실행 방식** (D-214) — 사용자는 평일 07:15~저녁 부재, 텔레그램만 가능. m2-verify 초안 판정은 KST 10:00·13:00 | (가) 수동 run: 출발 전 시작, 계속 실행(저녁 종료 또는 24/7), 재시작 시 텔레그램 `/resume K8` / (나) M2 무인 자동 운영 먼저 만들기 — 별도 CP, PAPER-0 시작 지연 | (가) | **차단**(T-050·T-M2-15 운영 방식) |
| Q-3 | **M2 텔레그램 봇** (D-213) — M2 전용 봇 생성·토큰을 M2 체크아웃 비밀 설정에 직접 입력·텔레그램 연결 승인 | (가) T-050 전에 준비(부재 중 유일한 알림·재개 경로) / (나) 텔레그램 없이 시작(알림은 DB·콘솔만, K8 재개는 귀가 후) | (가) | 구현 비차단 / **T-050·T-M2-15 차단** |
| Q-4 | **B-6 시장가 매도 수량 단위** (D-208) — MARKET_LOT_SIZE stepSize 0이면 LOT_SIZE stepSize로 내리고 잔량은 dust | (가) 채택, PV-M2-08 실측이 다르면 CP / (나) 내리지 않고 전송해 실측 — 어댑터가 전송 전 실패로 막으므로 코드 변경도 필요, 매일 청산 실패 위험 | (가) | **차단**(T-046) |
| Q-5 | **승인 범위** — 이 CP 승인 = D-201~D-216·V-280~V-290·PV-M2-12·T-045~T-050 문서 반영 + T-045~T-049 오프라인 구현 착수. T-050 관찰 run의 **WS 사용자 스트림 구독·시세 주기 조회**를 A-096("조회만": 서버 시각·exchangeInfo·계좌·미체결·주문/체결 조회)에 포함해 볼지 | (가) T-050 직전에 테스트넷 연결 범위 확장을 따로 승인(WS API·스트림 호스트, 주문 차단 유지) / (나) A-096 범위로 해석 | (가) — A-096 열거에 WS 구독이 없음 | 구현 비차단 / **T-050 차단** |

이 CP 승인과 별개로 남는 사용자 결정(기존): M2 검증 전략 승인과 m2-verify v1 대 이전 초안(`m2-paper0-verify`) 선택(B-5), T-M2-15 테스트넷 주문 전송 승인, V-157 잔여 4개 처리.

## 7. 안전 검토
- **주문 경로 하나**(CLAUDE.md §8-2): 신호·운영 요청 → (M2 주문 조건 훅: 유형·가격만 정함, 게이트 전) → 게이트(G1~G15) → 실행기 → `BinanceSpotAdapter` → 스트림·대조·기록. 훅은 요청을 만들거나 버릴 뿐 게이트를 건너뛰지 않는다. 취소·청산·운영 요청도 같은 경로(D-211).
- **전략은 신호만**(§8-1): 판정 입력(D-207)은 읽기 전용 스냅샷이며 전략은 어댑터·네트워크·저장소를 부르지 않는다.
- **자동 재전송 없음**(§8-7): 주문 변경은 1회 전송(D-203), 429·5XX·타임아웃은 결과 불명. 조회 재시도는 최대 3회(D-113)·결과 불명 해소 5회 뒤 K3(D-111).
- **결과 불명**(§8-6): `SpotUnknownResolver` — 조회로 해소되기 전 같은 의도 재주문 없음, 미해소 K3·사람 확인. 전송 후 24시간 보류(D-111).
- **재시작**(§8-9): 시작은 항상 K8 → 시작 대조 → 사용자 재개(D-209). 원격 재개는 CP-026 조건 그대로.
- **정지는 청산이 아님**(§8-8): 어떤 경로도 지급 BTC·보유를 자동으로 팔지 않는다. Q-1 (가)는 사람이 내는 운영 요청이다.
- **불확실하면 주문하지 않음**(§8-10): 규칙 적재 실패·rateLimits 없음·규칙 검사 불일치 → 시작 거부. 시세 지연 → 위험 확대 거부.
- **모드·환경 분리**(§7): 테스트넷 호스트만(시작 일치 표), 실전·Demo 상수 없음, LIVE 시작 거부 유지, 주문 스위치는 접속 스위치와 별개이며 기본 꺼짐(D-204). M2 DB·잠금·KILL·하트비트는 M2 체크아웃 `var/paper/`(D-096).
- **M1 영향**: 엔진·조립 지점 변경은 M1 실행 코드다. M1 프로파일 = 현 객체(D-201), V-152 전체 회귀, 병합은 M1 자동 운영 종료 뒤 사용자 지시 시(D-216). M1 PAPER 운영 중(직접 매수분 K4 정리 대기, handoff)이라 병합 시점은 사용자가 정한다.
- **비밀**: 키·서명·쿼리 문자열·응답 원문을 로그·오류·감사에 넣지 않는다(T-M2-10 규칙 유지). 에이전트는 M2 비밀 설정을 읽지 않는다.
- **위험이 늘어나는 곳**: (1) 주문 차단 run에서 운영 `/exit`가 전송 전 실패로 끝나는 것은 의도된 동작(전송 0) — 사용자에게 시작 출력으로 알린다. (2) 공급 스레드·스트림 스레드 추가로 감시 대상이 늘어남 — 죽으면 K5(D-215).

## 8. 가정 · 미확인 · 차단 조건
| 구분 | 내용 | 필요 시점 | 차단 여부 | 확인 주체 |
|---|---|---|---|---|
| 사실 | `run --market M2` 거부(`main.py:241`), BrokerAdapter 바이낸스 구현 없음, WS 연결 코드 없음, M2 구성 요소 다수 src 미사용(§2) | — | — | 2026-10-15 grep |
| 사실 | 04 2-M2.6 PAPER-0 시작 조건 "M2 L1~L3 전부 통과" — V-157 미실행 4개(V-02·V-76 부분, V-112·V-127) | T-M2-15 전 | **차단**(T-M2-15) | 구현(T-M2-09 잔여) + 사용자 |
| 미결 | Q-1 지급 BTC 처리 | T-M2-15 전 | **차단** | 사용자 |
| 미결 | Q-2 실행 방식, Q-3 텔레그램 봇, Q-5 연결 범위 | T-050 전 | **차단**(T-050) | 사용자 |
| 미결 | Q-4 수량 단위 | T-046 착수 | **차단**(T-046) | 사용자 |
| 미확인 | MARKET_LOT_SIZE stepSize 0의 거래소 의미(B-6) | PV-M2-08 | 비차단(D-208로 보수 처리) | PV-M2-08 실측 |
| 미확인 | HMAC 키로 `userDataStream.subscribe.signature` 동작(B-U9) | T-050 | **차단**(불통과 시 A-063⑨ 키 유형 재결정) | PV-M2-06(T-050에서 관찰) |
| 미확인 | 기존 `websockets` 고정 버전으로 WS API 서명 구독 가능 여부(D-114 [추정]) | T-048 착수 | 비차단(새 설치 필요 시 승인 절차) | 에이전트(코드·문서 확인) |
| 미확인 | 최근가·avgPrice·referencePrice·klines 엔드포인트 weight와 D-206 주기 값의 예산 적정성 | T-048 | 비차단 | 에이전트(BN §8 원문 재확인) |
| 미확인 | 테스트넷 BTCUSDT 시세 수준·호가 유동성(B-U4 잔여) — Q-1 (가) 1 BTC 시장가 매도의 체결 수준 | T-M2-15 | 비차단(테스트넷 가상 자산, 위험 축소) | PV-M2-03 관찰 |
| 미확인 | 테스트넷 수수료 적용·수수료 자산(B-U10) | T-M2-15 | 비차단(미승인 수수료 자산은 K4, D-101) | PV-M2-07 |
| 가정 | M1·M2 프로세스를 같은 PC에서 동시에 돌려도 자원 충돌 없음(실행 디렉터리·잠금·DB 분리, 시계·회선 공유는 03 2-M2.1에 기록) | T-050 | 비차단 | 사용자 관찰 |
| 가정 | 사용자가 KST 07:15 전에 M2 run을 시작할 수 있음(Q-2 (가)) | T-050 | 비차단 | 사용자 |
| 관찰 | `spot_order_terms`(`trading_rules.py:174-180`)는 규칙·시세가 없으면 **매도 의도도 만들지 않는다** — D-106 "규칙 미적재 시 위험 축소 허용"과 다름. 이 CP는 바꾸지 않고(V-281 (e)는 진입만 검증) 별도 결정으로 올린다 | T-046 전 | 비차단(제안) | 사용자 |
| 관찰 | m2-verify 초안은 크립토 현물 CAP을 "번호 미정"으로 적었으나 00 v0.5(A-102 승인) §2.3에 **CAP-INS-05**로 정의되어 있다(`00-strategy.md:117`) — 초안 갱신 때 반영 | 전략 승인 전 | 비차단 | 에이전트 |
| 관찰 | m2-verify 초안 브랜치는 main 5a47a41 기준(CP-035·T-044 병합 전) — 병합 전 재기준 필요 | 전략 병합 전 | 비차단 | 에이전트 |

## 9. 승인 문서 영향 (제안 문구 — CP 승인 뒤 다음 draft에 반영, 지금은 이 문서에만)
| 문서·위치 | 현재 | 제안 |
|---|---|---|
| 03 v1.0 §2-M2.1 도식 아래 | (도식만) | "[CP-036] 실행 경로: 엔진은 시장 공통, 시장 프로파일(D-201)로 시각·세션·대조 일정·판정 입력·게이트 전 주문 조건·계좌 사실을 바꾼다. 조립은 `app/wiring.build_engine(market=M2)`(D-202). 어댑터 D-203, 사용자 스트림 D-205, 공급 스레드 D-206, 스레드·정지 연결 D-215" |
| 03 v1.0 §2-M2.2 정상 흐름 1 | 시작: 모드·market·broker·도메인·DB 메타 일치 → 서버 시각 → exchangeInfo → 계좌 조회 → 대조 → K8 재개 대기 | 같은 순서 + "주문 전송 스위치 확인(D-204), exchangeInfo·rateLimits 적재 실패 시 시작 거부, 전략 `validate_rules`(D-209)" |
| 03 v1.0 §2-M2.3 표 | — | 새 행: "주문 전송 스위치 꺼짐 → 주문 계열 전송 전 차단, 전략 배포 시 시작 거부(D-204)", "공급·스트림 스레드 사망 → K5(D-215)" |
| 03 v1.0 §2-M2.6 | 수량: stepSize 내림 | + "MARKET_LOT_SIZE stepSize 0이면 시장가 매도도 LOT_SIZE stepSize로 내림, 잔량 dust(D-208)" |
| 03 v1.0 §13 | `broker/binance/` 설명(서명·4분류·가중치·필터 문자열·스트림 파서) | + "[CP-036] 어댑터(BrokerAdapter 구현)·WS 사용자 스트림 연결·시세 공급(파일 이름은 T-047·T-048에서)". 새 최상위 폴더 없음 |
| 04 v0.9 2-M2.3 | V-129~V-161 | + V-280~V-290(§5 표) |
| 04 v0.9 2-M2.6 PAPER-0 시작 조건 | M2 L1~L3 전부 통과, 시작 점검 통과, 검증 전략 승인, 유니버스 결정 | + "M2 실행 경로 V-280~V-290 통과, 테스트넷 관찰 run(PV-M2-12) 결과 기록, 지급 자산 처리(CP-036 Q-1) 결정, 테스트넷 주문 전송 승인(주문 스위치는 사용자가 설정)" + PV 표에 PV-M2-12 행 |
| 04 v0.9 §4.1 PAPER-0 검증 전략 예외 | M1(1주 단위 시장가) | + "M2판(D-212): markets M2, S-1·S-4, 능력 CAP-TRG-01·DAT-01·ORD-01·ORD-02·INS-05, l7 필수, 승인 A-ID" |
| 05 v0.6 작업 표 | T-M2-14 다음 T-M2-15 | T-045~T-050 행 추가(§4 표), T-M2-15 선행 열에 "T-050, 테스트넷 주문 전송 승인, CP-036 Q-1" 추가 |
| 05 v0.6 의존 도식 | `T-M2-01~T-M2-14 ─▶ T-M2-15 (PAPER-0)` | `T-M2-01~T-M2-14 ─▶ T-045 ─▶ (T-046 ∥ T-047) ─▶ T-048 ─▶ T-049 ─▶ T-050(관찰, 별도 연결 승인) ─▶ T-M2-15 (PAPER-0, 주문 전송 승인)` |
| 05 v0.6 사용자 준비 표 | T-M2-13 전 M2 봇 | + "T-050 전: M2 봇 토큰 입력·텔레그램 연결 승인, 테스트넷 연결 범위 확장 승인(Q-5)" / "T-M2-15 전: `orders_approved`·`orders_approval` 직접 설정" |
| 01 v0.9 시작 조건(R-01 관련 문단) | PAPER는 모의 접속 대상과 계좌 유형 확인 뒤 시작 | + "[M2] 테스트넷 주문은 접속 승인과 별도의 주문 전송 설정(승인 A-ID 포함)이 있을 때만 — 없으면 주문 차단 run(D-204)" |
| capabilities.md | M2 열 전부 미지원, CAP-INS-05 행 없음 | D-212 (3) 값. 반영은 T-049 통과 + 문서 승인 + M1 run 꺼진 시간 |
| broker-specs/binance-spot.md §6 | MARKET_LOT_SIZE "같은 형태 규칙" | + "step 0 처리 = LOT_SIZE 내림(CP-036 D-208, 거래소 의미 미확인 B-6 → PV-M2-08)" |
| user-manual | (M2 없음) | M2 run 시작·정지·`/resume K8`·기준선 확정·리셋 확인·주문 스위치 절차(복사해 붙일 전체 명령과 기대 결과) — T-049·T-050 때 |

## 10. 추적
| R-ID | D-ID | T-ID | 검증 항목 |
|---|---|---|---|
| R-04, R-14, R-26 | D-201, D-207 (CP-024 D-108) | T-045, T-046 | V-280, V-152 |
| R-04, R-16, R-28 | D-201, D-208, D-211 (CP-024 D-102·D-104·D-120) | T-045, T-046, T-049 | V-281, V-282, V-290 |
| R-01, R-24 | D-202, D-209 (CP-024 D-097·D-113·D-115) | T-045, T-046 | V-283 |
| R-05, R-06, R-09, R-11, R-12 | D-203, D-204 (CP-024 D-098·D-110~D-113) | T-047 | V-284, V-285 |
| R-08, R-09, R-22 | D-205, D-206, D-215 (CP-024 D-106·D-109·D-114·D-116) | T-048 | V-286, V-287 |
| R-01, R-08 | D-210 (CP-024 D-099·D-103) | T-049 | V-288 |
| R-17, R-20 | D-212 (00 D-004, DP-24 a) | T-049 | V-289 |
| R-13, R-21 | D-211, D-213 (CP-024 D-117) | T-049 | V-290 |
| R-01, R-08, R-09 | D-204, D-214, D-216 | T-050 | PV-M2-12 (PV-M2-05·06 일부) |

## 11. 다음 단계 입력
- 사용자: §6 Q-1~Q-5 답, 이 CP 승인 여부. 승인되면 approvals.md에 A-ID 기록, 03·04·05(+01·capabilities·binance-spot) 다음 draft에 §9 문구 반영 → 문서별 승인.
- 구현(승인 + 착수 지시 뒤): T-045부터 순서대로. 첫 작업은 fake 최소 종단 간 run(네트워크 없음). M1 전체 회귀 건수를 매 작업 보고.
- 병합: T-045~T-049 main 병합은 M1 자동 운영 종료 뒤 사용자 지시 시. `m2/run` 갱신도 사용자 지시 시(사용자 로컬 설정 수정 보존 확인).
- 별도 승인 필요: T-050 테스트넷 연결 범위(Q-5), M2 텔레그램 연결(Q-3), T-M2-15 테스트넷 주문 전송 + 검증 전략.
- m2-verify 초안 갱신 입력: CAP-INS-05 선언, 판정 입력 요구를 D-207·D-208 기준으로 정리, main 최신 기준으로 재기준.
- 별도 제안 후보: `spot_order_terms` 규칙 미적재 시 매도 처리(§8 관찰), M2 무인 자동 운영 CP(Q-2 (가) 뒤 관찰 결과로), M2 백테스트 판정 시각 복수화.

## 12. 변경 이력
| 버전 | 일자 | 변경 문단·표 행 | 영향 ID | 승인 |
|---|---|---|---|---|
| v0.1 | 2026-10-15 | 최초 작성(현황 인벤토리, 결정 D-201~D-216, 작업 T-045~T-050, 검증 V-280~V-290·PV-M2-12, 질문 Q-1~Q-5) | D-201~D-216, V-280~V-290, PV-M2-12, T-045~T-050, 03 2-M2·§13, 04 2-M2.3·2-M2.6·§4.1, 05 작업 표·의존 도식, capabilities.md | 미승인 |
