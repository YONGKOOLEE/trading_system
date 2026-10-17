# crypto-trend v1 운영 검토 — 복사·구조 시험·M2 등록 가능 여부·재현 가능 여부

- 문서: `docs/strategies/crypto-trend/review-v1.md` / 버전 v0.1 / 상태 **draft (미승인)** / 작성 2026-10-18 에이전트(T-M2-17 후보 검토, 브랜치 `feat/crypto-trend-v1-review`, 기준 커밋 434211a = `feat/T-052-053-m2-periodic`)
- 근거 지시: **A-118 A**(사용자 "진행", 2026-10-18) — 복사·구조 시험·M2 등록 가능 여부(CAP-TRG-02). **재현·공식 판정표는 장중 백테스트(T-068 등) 준비 뒤.** 등록·PAPER·유니버스(A-096)·한도 변경은 각각 별도 승인 — 이 작업은 어느 것도 하지 않았다(설정·한도 파일·capabilities.md 무변경).
- **생존편향·사후 설계 포함 — 전략실 수치는 모두 과대 추정으로 읽는다**(BTC·ETH 사후 선택, 게이트·1시간 판정은 결과를 본 뒤 설계 — 전략실 자체 표시).
- 관련 ID: T-M2-17, A-118, CP-037 D-219·D-220·D-221·D-222(A-113), CP-040 D-258·D-259·D-262·D-265·T-066·T-068(A-115), CP-036 D-209·D-212, DP-54, A-064·A-096·A-110, CP-030 C-1~C-8.

## 1. 복사 (요청 1)

| 운영 경로 | 전략실 원본 (읽기만, 커밋 08ebd6f) | sha256 앞 16자리 | 운영 수정 |
|---|---|---|---|
| `src/trading_system/strategy/crypto_trend.py` | `candidates/crypto_trend/strategy.py` | `2a2b6ad2510cf069` | **없음** (바이트 비교 동일) |
| `config/strategies/crypto-trend-v1.toml` | `params-v1.toml` | `d3d0779f4350f338` | 없음 |
| `config/strategies/crypto-trend-v1-paper.toml` | `params-v1-paper.toml` | `0a017bcc195bc48d` | 없음 |
| `docs/strategies/crypto-trend/strategy.md` | `strategy.md` | `f2fa5d087c49338f` | 머리 안내 1단락만 |
| `docs/strategies/crypto-trend/lab-handoff.md` | `handoff.md` | `5573bf755ed25478` | 머리 안내 1줄만 |
| `docs/strategies/crypto-trend/lab-notes.md` | `notes.md` | `cd2c7b2515e97766` | 머리 안내 1줄만 |
- 원본 md는 CRLF, 저장소는 `.gitattributes`(`* text=auto eol=lf`)로 LF. `trials.jsonl`은 복사하지 않았다(시도 수는 lab-notes.md·lab-handoff.md에 요약됨).
- btc-trend 때와 달리 **불가피 수정(F-1형)이 없었다** — 전략실이 btc-trend F-1~F-3 통지를 반영해 처음부터 CAP-INS-05·validate_rules·validate_deploy를 넣었다.

## 2. 구조·능력 (요청 1) — 코드와 대조

| 항목 | 명세 | 코드 확인 | 판정 |
|---|---|---|---|
| 능력 | CAP-TRG-02·CAP-DAT-01·CAP-ORD-01·CAP-INS-05 | 시험이 집합 일치 확인. CAP-INS-01·CAP-TRG-01·CAP-DAT-02·CAP-HLD-02 없음 | 일치 |
| 상품 | CAP-INS-05 크립토 현물 | CAP-INS-01로 바꾸면 BACKTEST 등록부터 거부됨을 시험으로 확인 | 일치 |
| 입력 | 확정 일봉 + 판정 시세(last_price) + 상태 입력 | AST: evaluate가 읽는 종목 속성 = `instrument·data_ok·has_pending_order·daily_bars·last_price·position·statuses`뿐(`minute_bars`·`minute_ok` 없음). 분봉을 넣어도 신호 불변, 일봉 없음·50개 미만이면 신호 없음 | 일치 — CAP-DAT-02·CAP-HLD-02 미선언이 맞다 |
| 규칙 표 | EXIT-2(1) > EXIT-1(2), ENTRY-1(1), 트리거 CAP-TRG-02, 입력 (CAP-DAT-01, 상태 입력) | 등록 검사(선언 CAP-ID·상태 입력만) 통과 | 일치 |
| 구조 규칙 | 전략은 domain만 import, 시계·네트워크·파일 없음 | `tests/architecture` 통과(전략 모듈 자동 검사 대상) | 통과 |
| validate_rules·validate_deploy | 있음(btc-trend F-2·F-3와 같은 서명) | 있음 — 단 아래 F-2·F-3 | 있음(결함 있음) |
| 규칙 동작(가상 봉) | 돌파 + SMA 게이트 진입, S-3 수량 종목별 내림, 손절가, EXIT-1, EXIT-2 우선, 수동 보유 무시, 두 종목 독립 | 시험 통과. PAPER 축소판 주문 금액 ≤ L7 100 USDT·≥ minNotional 5(가격 4점) | 통과 |

## 3. M2 등록 가능 여부 (요청 2) — 이 브랜치(T-052 CAP-TRG-02 "검증 중")

| 단계 | 결과 | 근거(시험) |
|---|---|---|
| 등록 검사 BACKTEST (`validate_spec`, capabilities.md M2 열) | **통과** — CAP-TRG-02·DAT-01·ORD-01·INS-05 모두 "검증 중" → BACKTEST 허용 | `test_registration_backtest_passes_with_trg02_verifying_and_paper_refused` |
| 등록 검사 PAPER | **거부** — 능력 4개 모두 "검증 중 — PAPER 등록 불가". M2 PAPER-0 예외(D-212)는 S-1·S-4·CAP-TRG-01 범위라 적용 불가 | 같은 시험 |
| M2 조립: 유니버스 | **거부** — `유니버스 밖 심볼 ETHUSDT`(A-096 = BTCUSDT만). ETHUSDT 거래 규칙 실측도 없음 | `test_m2_assembly_refuses_ethusdt_outside_universe_a096` |
| M2 조립: 주기 격자(D-219) | **거부** — `CAP-TRG-02 전략은 정수 파라미터 period_minutes이 필요` (F-1) | `test_period_grid_check_refuses_missing_period_minutes_now` |
| 배포 검사 validate_deploy | 판정 시각 23개만 통과, `eval_times = []`는 거부 → D-219(CAP-TRG-02는 `[]`)와 **동시에 만족 불가** (F-2) | `test_validate_deploy_requires_23_times_now` |
- 결론: **BACKTEST 등록 검사는 통과하지만, 실운영 조립·1분봉 실행기에는 현재 올릴 수 없다**(F-1·F-2, 그리고 ETHUSDT 유니버스). PAPER는 능력 "지원" 승격(T-M2-16·T-056 뒤) + 유니버스 결정(B) + F-1·F-2 보강 전까지 불가.

## 4. 결함·관찰 (전략 로직은 고치지 않음 — xfail strict로 기록)

| ID | 내용 | 영향 | 처리 제안 |
|---|---|---|---|
| **F-1** | 파라미터 `period_minutes` 없음. 이 브랜치 D-219 구현(`app.wiring.period_grid_of`)과 CP-040 D-258(A-115 Q-1: M2 등록은 `period_minutes ∈ {15, 60}`)은 주기 판정 일정을 **전략 파라미터**로 받는다. 전략실은 "60분"을 상수(`PERIOD_EVALS_PER_DAY = 23`)·설명으로만 표현 | 실운영 조립·1분봉 실행기 모두 데이터 읽기 전 거부 | ParamSpec `period_minutes`(int, 60~60 또는 {60}) + 두 파라미터 파일에 `period_minutes = 60` 추가. **거래 논리 무변경이지만 명세·파라미터 표면 변경** → 전략실 반영 또는 운영 보강(btc-trend A-114 ③ 선례)은 사용자 결정 |
| **F-2** | `validate_deploy`가 판정 시각 정확히 23개를 요구. D-219는 CAP-TRG-02 전략의 `eval_times`를 빈 목록으로 강제 | 어떤 배포 설정도 두 검사를 함께 통과할 수 없음. 1분봉 CLI는 `validate_deploy(params, ())`를 불러 거부 | `eval_times == ()`와 `period_minutes == 60`을 요구하도록 변경(+ M2 전용 검사 유지). 전략실 docstring도 "배포 표현은 CP-040 구현에 맞춰 조정"이라 적음 |
| **F-3** | `validate_rules(params, lot_step, min_notional)` 서명에 종목이 없어 "어느 종목 단위와도 같으면 통과" — BTCUSDT에 ETH 단위 0.0001이 와도 통과(시험으로 확인). 전략실 자체 표시 항목 | 종목별 단위 불일치를 시작 검사가 놓침(CLAUDE.md §8-10 취지) | DP-54 서명에 종목 추가(시스템 공통 변경 — CP) 또는 전략이 종목별 단위를 파라미터로 받기. ETH 0.0001은 가정 — 거래소 exchangeInfo 확인 필요 |
| O-1 | 일봉 실행기(`app.backtest_m2`)에 판정 시각 23개를 주면 **모든 판정의 판정 시세 = 그날 시가**, 지표 = 전날까지 봉(docstring·`backtest_m2.py:466`). 1시간마다 다른 last_price가 없으므로 23회 판정은 사실상 "하루 1회 시가 판정"으로 줄어든다 | 일봉 실행기로는 이 전략의 핵심(장중 조기 진입·청산)을 볼 수 없음 | §5 근사 실행은 이 한계를 표시 |
| O-2 | 1분봉 실행기(T-053)는 단일 심볼(`--symbol`), `daily_bars ()`(분봉 전략 입력만, [제안] 표시), 최근가 = 직전 닫힌 1분봉 종가 | F-1·F-2를 고쳐도 일봉 입력이 없어 신호 0(시험 `test_minute_engine_input_shape_gives_no_signal`), ETH 동시 운용 불가 | T-068 범위 확인(§6, 질문 1) |
| O-3 | 실운영 M2 스냅샷(`app.market_profile._view`)은 주기 판정에서도 확정 일봉(`final_daily_bars`) + 시세(last_price)를 준다 | 실운영 입력 모양은 이 전략과 맞는다 — 문제는 일정 표현(F-1·F-2)·유니버스·능력 상태 | 기록 |
| O-4 | 손절 정의 3가지(신호 손절가 / strategy.py EXIT-2 평균 매입가 − 2×현재 ATR / 전략실 시뮬레이터 진입 ATR)와 공유 현금 동시 신호 처리 순서는 전략실이 명시한 미확인 | 재현 시 거래 수·수익 차이 원인 후보 | T-068 재현 때 원인 분석 |

## 5. 재현 가능 여부 (요청 3)

### 5.1 필요한 것과 현재 엔진
전략실 결과(2017-09-01~2025-12-31, 23회 일정, BTC·ETH 한 계좌)를 충실히 재현하려면 실행기가 **동시에** 다음을 해야 한다.

| 필요 | `app.backtest_m2` (일봉, 여러 판정 시각) | `app.backtest_m2_minute` (T-053 1분봉) |
|---|---|---|
| 60분 격자 판정(01:00~23:00 23회, D-219·D-258) | 시각은 받지만 판정 시세가 하루 내내 시가 — **불가**(O-1) | **가능** (PeriodGrid) — 단 F-1·F-2로 이 전략 거부 |
| 판정 시점 last_price(직전 구간 마지막 가격) | 불가 | 가능(직전 닫힌 1분봉 종가 — 전략실 "직전 1시간 봉 종가" 근사와 같은 뜻) |
| 확정 UTC 일봉 입력(daily_bars, 50개 이상) | 가능 | **불가** — `daily_bars ()` |
| 두 심볼 한 계좌(공유 현금·처리 순서) | 함수는 `data` 사전으로 여러 심볼 가능, CLI는 단일 심볼·BTCUSDT 규칙만 | **불가** — 단일 심볼 |
| 다음 분 시가 체결·IOC 한도·결측 미체결(D-221) | 일봉 근사(첫 판정 시가, 이후 종가) | 가능 |
| 장중 L4·K2(D-265, A-115 Q-3 매분 저가) | 불가(판정 시점 한 번) | 판정마다 검사(매분 저가 아님 — T-068 몫) |
- **결론: 두 엔진 모두 충실한 재현이 불가능하다.** 우회 구현(전략실 재생 스크립트를 운영에 가져오기, 일봉을 1분봉 실행기에 임시 주입 등)은 하지 않았다.
- **필요한 것 = 1분봉 실행기에 (a) 여러 심볼 한 계좌, (b) 판정 시각 기준 확정 UTC 일봉 입력(1d 파일 또는 1분봉에서 만든 일봉 — 어느 쪽인지·일치 검사 필요), (c) CAP-TRG-02 60분 격자 + last_price, (d) 매분 저가 L4·K2(D-265)** — CP-040 T-068(장중 백테스트·검증 실행기 확장) 범위. 단 T-068 표의 산출물 문구(집계 봉 입력·손절 감시 체결·매분 L4·K2 통계)에 **(a)·(b)는 명시되어 있지 않다** — D-265 (4) "일봉 전략도 1분 데이터가 있으면 같은 장중 측정"이 (b)를 함의하지만 범위 확인이 필요(질문 1). 그리고 F-1·F-2 보강이 선행되어야 한다.

### 5.2 일봉 근사 실행 (재현 아님 — 엔진 호환 확인용)
**근사 — 재현 아님.** 일봉 실행기는 23회 판정 모두 그날 시가를 쓰므로(O-1) 결과는 "하루 1회 시가 판정 + BTCUSDT 단독"의 근사다. 목적은 운영 CLI가 이 전략을 등록·배포 검사·거래 규칙 검사를 거쳐 끝까지 돌리는지와 PAPER 축소판이 A-064 한도 안에서 거부 없이 움직이는지 확인하는 것뿐이다. 결과는 공식 판정·전략실 수치 비교에 쓰지 않는다.

- 명령(2026-10-18 실행, 출력은 작업 트리 밖 임시 폴더 — `var/` 미사용·미커밋):
  `python -m trading_system.app backtest --market M2 --strategy trading_system.strategy.crypto_trend:CryptoTrendV1 --params config/strategies/crypto-trend-v1-paper.toml --data <binance-public-20261017>/BTCUSDT-1d.csv --symbol BTCUSDT --start 2017-09-01 --end 2025-12-31 --run-id ct-v1-paper-daily23-btc --eval-time 01:00,02:00,…,23:00 --slippage 0.0005` (한도 = 기본 `config/m2/limits.paper.toml` A-064, 수수료 = 기본 0.1%, 초기 10,000 USDT)
- 결과: 종료 코드 0, 445.8초. 전략 등록 PASSED(BACKTEST), validate_deploy 통과(23개), 거래 규칙 검사 통과. 3,044일, 판정 70,012회, 신호 54, 체결 매수 27·매도 27, **왕복 27건**, 거부 0, K2 0, 수익 +7.78%(MDD 1.48%, 승률 44.4%, 손익비 11.95, 평균 보유 42일). 재현성(D-074) 통과, 룩어헤드 교란(V-91) 통과(문제 0). 데이터 sha256 앞 16자리 `32c6545d63e7ba8c`, 2026 이후 289행 제외, 불완전 봉 2018-02-08 제외. 지문 `5d8ddd45a7173fa3…`.
- 해석: 왕복 27건은 전략실 운영 엔진 일봉판(2종목 55건 중 BTC 27건, [전략실 보고])과 같은 수 — 엔진 적재·동작은 일치. 60분 판정의 효과(전략실 주장의 핵심)는 이 실행으로 확인되지 않는다. ETHUSDT는 거래 규칙·유니버스가 없어 돌리지 않았다(우회 없음).

### 5.3 전략실 수치 — [전략실 보고] (운영 미재현, 과대 추정)
| 항목 | 값 | 출처 |
|---|---|---|
| 2종목 23회 일정 2017-09~2025 재생(research/crypto_trend_replay.py — 운영 도메인 객체로 1시간마다 evaluate, 다음 봉 시가 체결, **운영 엔진 아님**) | **105건·+6,964%·MDD 33.5%**, 학습 2017-09~2021 비율 3.28, 후기 2022~2025 1.33 | lab-handoff.md·strategy.md |
| 7창(2019~2025) 사전 판정표(재생, 한도 미적용) | C-2 87건(6창 76) 미달, C-3 슬리피지 0.2% +345 USDT/건 충족, C-4 ±20% 10변형 충족, 손실 창 1/7 | 같음 |
| 운영 `app.backtest_m2` 2종목 **일봉판**(전략실 two_coin_engine, 하루 1회 00:15) | v1·가정 한도(L1 2.75%) 55건·+2,840%·MDD 28.9%·거부 0 / PAPER 축소판 + A-064 55건·+13.5%·거부 0 | 같음 |
- 운영 공식 판정 창은 CP-037 D-222 **M2 6창(2020~2025)** — 전략실 7창과 다르다. 공식 판정표(C-1~C-8)·C-2 처리(CP-038)·L4 1%에서의 K2 영향(V-334)은 T-068 뒤.

## 6. 재현 뒤 적용할 전략실 "폐기 기준" (lab-notes.md 맨 위, 사전 등록 2026-10-18 01:39 — 그대로 적용)
1. 엔진 7창(또는 운영 6창) **C-3**(슬리피지 0.2% 거래당 기대값 > 0) 또는 **C-4**(±20% 변형 모두 > 0) 미충족 → **폐기**.
2. 엔진 전체 구간(2017-09~2025) 거래 수가 전략실 재생 **105건과 ±15%**(약 89~121건) 넘게 다르거나 최종 수익이 **±30%** 넘게 다르면 → 원인 분석 전 **인계 보류**(같은 규칙인지 먼저 확인).
3. 운영 **L4 1%** 적용 시 거래 수가 재생 대비 **30% 넘게** 줄면 → "한도가 결과를 지배" 표시, L4 값은 사용자 결정으로 넘김.
- 시도 상한: v1 버전당 엔진 시도 5회(라벨별 기록). 규칙·파라미터 변경 시 v2(이전 결과 승계 없음, D-011). 이번 작업의 일봉 근사(§5.2)는 장중 엔진 시도가 아니므로 이 5회에 넣지 않는다 [제안].
- 주의: (2)의 "최종 수익 ±30%"는 +6,964% 기준이면 약 +4,875%~+9,053% — 비교 조건(한도·수수료·슬리피지·초기 금액)을 전략실 재생과 맞춰야 의미가 있다.

## 7. 검증 기록
- 새 시험 `tests/scenario/test_tm2_17_crypto_trend.py`: 파라미터 2개, 명세·능력 집합, AST 입력 확인, 분봉 무영향·일봉 필수, 등록(BACKTEST 통과·PAPER 거부·CAP-INS-01 거부), 조립(ETHUSDT 거부·주기 격자 거부·validate_deploy 23개), F-1·F-2·F-3 xfail strict, 1분봉 CLI 거부(종료 코드 2, 데이터 읽기 전), 1분봉 입력 모양 신호 0, 규칙 동작 7개(PAPER 크기 4점 포함). 가상 가격.
- 실행(2026-10-18): `.venv\Scripts\python -m pytest -q tests/scenario/test_tm2_17_crypto_trend.py` → **20 passed, 3 xfailed** / `tests/architecture` → **27 passed** / 전체(`-m` 없음) → **3335 passed, 1 skipped, 3 xfailed, 실패 0** (19분 54초). 건너뜀 1 = 기존 test_t042_allocation.py:290, xfail 3 = F-1·F-2·F-3. 시험은 처음 실행에서 통과(전략 코드 무변경 — 현재 동작 기록 시험이라 "먼저 실패" 단계 없음).

## 8. 가정·미확인·차단
| 구분 | 항목 | 차단 대상 |
|---|---|---|
| 차단 | F-1·F-2(주기 일정 표현) | 실운영 조립·1분봉 실행 — 보강 방식은 사용자 결정 |
| 차단 | 장중 엔진 (a)~(d) (T-068 범위 확인 포함) | 재현·공식 판정표 |
| 차단 | 유니버스 ETHUSDT(A-096 변경, 결정 B) | M2 조립·PAPER |
| 차단 | M2 능력 "지원" 승격(T-M2-16·T-056 뒤) | PAPER 등록 |
| 미확인 | ETHUSDT LOT_SIZE 0.0001·minNotional(전략실 가정), F-3 | 시작 규칙 검사 정확도 |
| 미확인 | 전략실 1시간·일봉 봉(1분봉 "있는 분만" 묶음)과 운영 일봉(CAP-DAT-01) 일치 | 재현 차이 해석 |
| 미확인 | L1 ≥ 2.75%(잠정)·L4 영향(V-334) | LIVE 한도 — 제안만, 결정은 사용자 |

## 9. 다음 단계 입력
- 사용자: F-1·F-2 보강 주체(전략실 v1 수정 재인계 / 운영 보강 — 거래 논리 무변경), T-068 범위에 (a) 여러 심볼·(b) 일봉 입력 포함 여부, 유니버스 결정 B.
- 운영: T-066·T-068 구현 뒤 §6 폐기 기준으로 재현 → 공식 판정표(M2 6창).

## 변경 이력
| 버전 | 날짜 | 변경 | 영향 ID |
|---|---|---|---|
| v0.1 | 2026-10-18 | 최초 작성 | T-M2-17, A-118 |
