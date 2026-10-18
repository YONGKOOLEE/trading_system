# crypto-trend 운영 검토 — v1 검토(F-1~F-3) → v2 재검토(F-1·F-2 해소 확인)

- 문서: `docs/strategies/crypto-trend/review-v2.md` (v0.1은 `review-v1.md`, 커밋 9fdb517 — 이름을 바꿔 이어 씀) / 버전 v0.2 / 상태 **draft (미승인)** / 작성 2026-10-18 에이전트(T-M2-17 후보 검토, 브랜치 `feat/crypto-trend-v1-review`, v1 검토 기준 커밋 434211a = `feat/T-052-053-m2-periodic`)
- 근거 지시: **A-118 A**(v1 복사·구조 시험·M2 등록 가능 여부), **A-119 ①**(2026-10-18, crypto-trend v2 운영 재검토 착수 — 복사·구조 시험·등록 확인. **등록·PAPER 아님**). 재현·공식 판정표는 장중 백테스트(T-068 확장) 뒤. 등록·PAPER·유니버스(A-096, A-119 ③은 F-3 반영 뒤)·한도 변경은 이 작업에서 하지 않았다(유니버스·한도 파일·capabilities.md 무변경).
- **생존편향·사후 설계 포함 — 전략실 수치는 모두 과대 추정으로 읽는다**(BTC·ETH 사후 선택, 게이트·1시간 판정은 결과를 본 뒤 설계 — 전략실 자체 표시).
- 관련 ID: T-M2-17, A-118, A-119 ①②③, CP-037 D-219·D-220·D-221·D-222(A-113), CP-040 D-258·D-259·D-262·D-265·T-066·T-068(A-115), CP-036 D-209·D-212, DP-54, A-064·A-096·A-110, CP-030 C-1~C-8.

## 0. v2 재검토 요약 (A-119 ①, 2026-10-18)

| 항목 | v1 (9fdb517) | v2 (이번) |
|---|---|---|
| 전략실 원본 | 커밋 08ebd6f `CryptoTrendV1` | 커밋 a5c1405 `CryptoTrendV2` (v1은 전략실 `strategy-v1.py`로 보존) |
| 로직 | — | v1 대비 바뀐 곳: 머리 설명·`VERSION` 1→2·상수 `PERIOD_EVALS_PER_DAY`→`PERIOD_MINUTES`·ParamSpec `period_minutes`(int 15~60) 추가·클래스 이름·`validate_deploy`. **`evaluate`·`validate_rules`·`_sig`·지표 함수 무변경**(diff 확인), 규칙 동작 시험(6개 함수·9건) 그대로 통과 |
| F-1 주기 격자(D-219·D-258) | 거부 (period_minutes 없음) | **해소** — `period_grid_of` 통과, 60분 격자 01:00~23:00 하루 23회. 실운영 등록 경로(`_register_strategies`, M2·BACKTEST)에서 `LoadedStrategy.period` = 60분 |
| F-2 validate_deploy ↔ D-219 | 동시 만족 불가 (23개 요구) | **해소** — `eval_times []` + `period_minutes 60` + M2만 통과. 판정 시각 23개·15분·M1·period_minutes 없음·bool 거부 |
| F-3 validate_rules 종목 구분 | 결함 | **남음** (xfail strict) — 전략 안에서 해결 불가, A-119 ② 공통 CP(서명에 종목 추가) 대기 |
| 등록 BACKTEST / PAPER | 통과 / 거부 | 같음 — 통과(CAP-TRG-02 등 "검증 중") / 거부(능력 4개 "검증 중 — PAPER 등록 불가") |
| M2 조립 유니버스 | ETHUSDT 거부(A-096) | 같음 — A-119 ③은 F-3 반영 뒤 설정 변경(순서 고정) |
| 1분봉 실행기(T-053) | 데이터 읽기 전 거부(F-1) | **받아서 끝까지 실행** — 단 일봉 입력이 없어 신호 0(가상 상승 1분봉 2일: 판정 46회, 신호 0) → **재현 아님** |
| 일봉 실행기(`app.backtest_m2`) | 23개 판정 시각으로 실행(근사) | **거부** — v2 `validate_deploy`가 판정 시각 23개를 막고, 일봉 CLI는 빈 판정 시각을 받지 않는다(§5.2) |

## 1. 복사

| 운영 경로 | 전략실 원본 (읽기만, 커밋 a5c1405의 커밋 내용) | sha256 앞 16자리 | 운영 수정 |
|---|---|---|---|
| `src/trading_system/strategy/crypto_trend.py` | `candidates/crypto_trend/strategy.py` | `6775310b2b416055` | **없음** (바이트 비교 동일) |
| `config/strategies/crypto-trend-v2.toml` | `params-v2.toml` | `2bc4d042de88d716` | 없음 |
| `config/strategies/crypto-trend-v2-paper.toml` | `params-v2-paper.toml` | `a8607cb0b9ca46a7` | 없음 (원본 마지막 줄의 order_cap 주석이 period_minutes 줄 뒤에 붙어 있음 — 값에는 영향 없음, 원본 그대로 둠) |
| `docs/strategies/crypto-trend/strategy.md` | `strategy.md` | `c05354f6b4dd35d0` | 머리 안내 1단락만 |
| `docs/strategies/crypto-trend/lab-handoff.md` | `handoff.md` | `ae44ccd134ba4029` | 머리 안내 1줄만 |
| `docs/strategies/crypto-trend/lab-notes.md` | `notes.md` | `c6a4918267c386ac` | 머리 안내 1줄만 |
- v1 사본(`crypto_trend.py` v1, `crypto-trend-v1{,-paper}.toml`)은 v2로 대체했다 — v1 클래스를 쓰는 다른 시험·설정이 없어 v1 코드를 따로 두지 않았고, v1 파라미터 파일은 v2 명세(version 2)로 적재되지 않으므로 삭제했다. v1 사본·시험 이력은 커밋 9fdb517에 있다.
- 전략실 작업 트리는 이후 커밋 031890f·7e011c1에서 `strategy.py` 주석 1줄(ETHUSDT 단위 "테스트넷 확인값")과 문서(ETH 규칙 확인 반영, `review-2026-10-18.md` — v3 후보·"v3 1순위, v2는 T-068 뒤" 권장)를 바꿨다. **지시(a5c1405) 기준으로 복사했고 이 변경은 포함하지 않았다** — 코드 차이는 주석뿐(로직 동일).
- `trials.jsonl`은 복사하지 않았다(시도 수는 lab-notes.md·lab-handoff.md에 요약됨).

## 2. 구조·능력 — 코드와 대조 (v2에서 다시 확인)

| 항목 | 명세 | 코드 확인 | 판정 |
|---|---|---|---|
| 능력 | CAP-TRG-02·CAP-DAT-01·CAP-ORD-01·CAP-INS-05 | 시험이 집합 일치 확인. CAP-INS-01·CAP-TRG-01·CAP-DAT-02·CAP-HLD-02 없음 | 일치 |
| 상품 | CAP-INS-05 크립토 현물 | CAP-INS-01로 바꾸면 BACKTEST 등록부터 거부됨을 시험으로 확인 | 일치 |
| 입력 | 확정 일봉 + 판정 시세(last_price) + 상태 입력 | AST: evaluate가 읽는 종목 속성 = `instrument·data_ok·has_pending_order·daily_bars·last_price·position·statuses`뿐(`minute_bars`·`minute_ok` 없음). 분봉을 넣어도 신호 불변, 일봉 없음·50개 미만이면 신호 없음 | 일치 — CAP-DAT-02·CAP-HLD-02 미선언이 맞다 |
| 파라미터 | 7개(v1 6개 + period_minutes) | 두 파일 모두 적재, `period_minutes` = 정수 60 | 일치 |
| 규칙 표 | EXIT-2(1) > EXIT-1(2), ENTRY-1(1), 트리거 CAP-TRG-02, 입력 (CAP-DAT-01, 상태 입력) | 등록 검사 통과 | 일치 |
| 구조 규칙 | 전략은 domain만 import, 시계·네트워크·파일 없음 | `tests/architecture` 27 통과(전략 모듈 자동 검사 대상) | 통과 |
| validate_rules·validate_deploy | 있음 | validate_deploy는 D-219 규약(F-2 해소). validate_rules는 F-3 남음 | F-3만 남음 |
| 규칙 동작(가상 봉) | 돌파 + SMA 게이트 진입, S-3 수량 종목별 내림, 손절가, EXIT-1, EXIT-2 우선, 수동 보유 무시, 두 종목 독립 | 시험 통과(v1과 같은 기대값). PAPER 축소판 주문 금액 ≤ L7 100 USDT·≥ minNotional 5(가격 4점) | 통과 |

## 3. M2 등록 가능 여부 — 이 브랜치(T-052 CAP-TRG-02 "검증 중")

| 단계 | v2 결과 | 근거(시험) |
|---|---|---|
| 등록 검사 BACKTEST (`validate_spec`, capabilities.md M2 열) | **통과** — 능력 4개 모두 "검증 중" → BACKTEST 허용 | `test_registration_backtest_passes_with_trg02_verifying_and_paper_refused` |
| 등록 검사 PAPER | **거부** — 능력 4개 모두 "검증 중 — PAPER 등록 불가". M2 PAPER-0 예외(D-212)는 적용 불가 | 같은 시험, `_register_strategies` PAPER 저장소에서도 "전략 등록 거부" |
| 실운영 등록 경로 (`_register_strategies`, M2·BACKTEST) | **통과** — 등록부 검사 → 파라미터 검증·형 변환 → `period_grid_of` → `LoadedStrategy(period=60분, eval_times=())` | `test_operational_registration_path_builds_60min_grid_in_backtest` |
| 주기 격자 (`period_grid_of`, D-219·D-258) | **통과** (F-1 해소) — 두 파라미터 파일 모두 60분·23회(01:00~23:00). `eval_times` 23개면 거부, M1이면 거부 | `test_f1_resolved_period_grid_gives_60min_grid_of_23_decisions`, `test_period_grid_still_refuses_eval_times_and_m1` |
| 배포 검사 validate_deploy | **`[]`·60·M2 통과** (F-2 해소) / 23개·15분·M1·없음 거부. 15분은 격자 검사(D-258 {15, 60})는 통과하지만 validate_deploy가 막는다 | `test_f2_resolved_validate_deploy_accepts_d219_periodic_deploy`, `test_period_15_passes_grid_but_validate_deploy_refuses` |
| M2 조립: 유니버스 | **거부** — `유니버스 밖 심볼 ETHUSDT`(A-096 = BTCUSDT만) | `test_m2_assembly_refuses_ethusdt_outside_universe_a096` |
| 시작 규칙 검사 (`spot_rule_problems`) | BTCUSDT 실측 단위 통과. 종목 구분 없음(F-3) | `test_start_rule_check_current_behaviour`, F-3 xfail |
- 결론: **v2는 BACKTEST 등록과 실운영 등록 경로의 일정 검사(D-219·D-258)를 통과한다.** 실운영 M2 조립에는 여전히 올릴 수 없다 — ETHUSDT 유니버스(A-119 ③, F-3 뒤)와 F-3. PAPER는 능력 "지원" 승격(T-M2-16·T-056 뒤) 전까지 불가.

## 4. 결함·관찰 (전략 로직은 운영에서 고치지 않음)

| ID | 내용 | v2 상태 | 처리 |
|---|---|---|---|
| **F-1** | 파라미터 `period_minutes` 없음 → D-219 주기 격자 검사 거부 | **해소** (전략실 v2: ParamSpec int 15~60, 두 파일 값 60) | xfail 시험 → 통과 시험 전환 |
| **F-2** | `validate_deploy`가 판정 시각 23개 요구 ↔ D-219 `eval_times []` | **해소** (v2: `[]` + 60 + M2 요구) | xfail 시험 → 통과 시험 전환 |
| **F-3** | `validate_rules(params, lot_step, min_notional)` 서명에 종목이 없어 "어느 종목 단위와도 같으면 통과" — BTCUSDT에 ETH 단위 0.0001이 와도 통과 | **남음** | xfail strict 유지 — **A-119 ② 공통 CP(DP-54 서명에 종목 추가) 대기**. ETHUSDT 단위 0.0001·minNotional 5는 운영이 테스트넷에서 확인(main 커밋 655b47d `binance-spot.md` §15 — 이 브랜치에는 아직 없음, 1분봉 실행기 `TESTNET_RULES`도 BTCUSDT만) |
| O-1 | 일봉 실행기(`app.backtest_m2`)는 여러 판정 시각을 받아도 모든 판정의 판정 시세 = 그날 시가 | v2는 일봉 실행기에서 **거부**됨(§5.2) — 근사 실행도 CLI로는 불가 | 기록 |
| O-2 | 1분봉 실행기(T-053)는 단일 심볼, `daily_bars ()`, 최근가 = 직전 닫힌 1분봉 종가 | v2는 실행되지만 신호 0 (`test_minute_backtest_cli_now_accepts_but_gives_no_signal`) | T-068 확장(A-119 ②) |
| O-3 | 실운영 M2 스냅샷(`app.market_profile._view`)은 주기 판정에서도 확정 일봉 + 시세를 준다 | 같음 — 실운영 입력 모양은 이 전략과 맞다 | 기록 |
| O-4 | 손절 정의 3가지·공유 현금 동시 신호 처리 순서(전략실 명시 미확인) | 같음 | T-068 재현 때 원인 분석 |

## 5. 재현 가능 여부

### 5.1 필요한 것과 현재 엔진 (v2에서도 같음)
전략실 결과(2017-09-01~2025-12-31, 23회 일정, BTC·ETH 한 계좌)를 충실히 재현하려면 실행기가 **동시에** 다음을 해야 한다.

| 필요 | `app.backtest_m2` (일봉, 여러 판정 시각) | `app.backtest_m2_minute` (T-053 1분봉) |
|---|---|---|
| 60분 격자 판정(01:00~23:00 23회, D-219·D-258) | 판정 시세가 하루 내내 시가 — **불가**(O-1). v2는 배포 검사에서 거부 | **가능** (PeriodGrid) — v2 통과 |
| 판정 시점 last_price | 불가 | 가능(직전 닫힌 1분봉 종가) |
| 확정 UTC 일봉 입력(daily_bars, 50개 이상) | 가능 | **불가** — `daily_bars ()` |
| 두 심볼 한 계좌(공유 현금·처리 순서) | CLI 단일 심볼 | **불가** — 단일 심볼 |
| 다음 분 시가 체결·IOC 한도·결측 미체결(D-221) | 일봉 근사 | 가능 |
| 장중 L4·K2(D-265 매분 저가) | 불가 | 판정마다 검사(매분 저가 아님 — T-068 몫) |
- **결론: 두 엔진 모두 여전히 충실한 재현이 불가능하다.** F-1·F-2 해소로 1분봉 실행기가 v2를 받게 되었지만, (a) 여러 심볼 한 계좌, (b) 판정 시각 기준 확정 UTC 일봉 입력, (d) 매분 저가 L4·K2가 없다 — **A-119 ② T-068 범위 확장**(1분봉 실행기에 여러 심볼 한 계좌·확정 일봉 입력, CP 초안부터). 우회 구현(전략실 재생 스크립트 가져오기, 일봉 임시 주입 등)은 하지 않았다.

### 5.2 일봉 근사 (재현 아님 — v1과 같은 동작인지 확인용)
**근사 — 재현 아님.** v2 `validate_deploy`는 판정 시각 23개를 거부하므로(F-2 해소의 결과) 운영 일봉 CLI로는 v1 때의 근사 실행을 할 수 없다:
- `python -m trading_system.app backtest --market M2 --strategy trading_system.strategy.crypto_trend:CryptoTrendV2 --params config/strategies/crypto-trend-v2-paper.toml --data <binance-public-20261017>/BTCUSDT-1d.csv --symbol BTCUSDT --start 2017-09-01 --end 2025-12-31 --eval-time 01:00,…,23:00 --slippage 0.0005 …` → 종료 코드 2, `배포 검사: CAP-TRG-02 전략의 eval_times는 빈 목록이어야 함(일정은 period_minutes, D-219) — 현재 23개`. `--eval-time ""` → 종료 코드 2(형식 오류). 둘 다 2026-10-18 실행.

v2 `evaluate`가 v1과 같게 움직이는지만 보려고, **작업 트리 밖 임시 모듈**(커밋 안 함)에서 `CryptoTrendV2`를 상속해 배포 검사만 v1 규칙(판정 시각 23개)으로 바꾼 클래스로 같은 명령을 실행했다(evaluate·파라미터는 v2 그대로, 출력은 작업 트리 밖 임시 폴더 — `var/` 미사용).
- 명령: 위와 같은 인자에 `--strategy ct_daily_approx:CryptoTrendV2DailyApprox`(PYTHONPATH에 임시 모듈 폴더 추가), `--run-id ct-v2-paper-daily23-btc`. 한도 = 기본 `config/m2/limits.paper.toml` A-064, 수수료 기본 0.1%, 초기 10,000 USDT, 슬리피지 0.0005.
- 결과(2026-10-18): 종료 코드 0. 등록 PASSED(BACKTEST, version 2), 3,044일, 판정 70,012회, 신호 54, 체결 매수 27·매도 27, **왕복 27건**, 거부 0, K2 0, 수익 +7.78%(최종 10,777.77 USDT, MDD 1.48%, 승률 44.4%, 손익비 11.95, 평균 보유 42일). 재현성(D-074) 통과, 룩어헤드 교란(V-91) 문제 0. 데이터 sha256 앞 16자리 `32c6545d63e7ba8c`, 2026 이후 289행 제외, 불완전 봉 2018-02-08 제외. **v1 근사(9fdb517 기록)와 신호·체결·왕복·수익 지표 모두 같다** — 지문은 `d02410280e62e171…`로 v1(`5d8ddd45a7173fa3…`)과 다르다— 지문 입력(신호 ID·체결·자산 곡선, `app.backtest`)의 신호 ID에 전략 버전이 들어가므로(`domain.models.signal_id_of`) 예상된 차이.
- 해석: 60분 판정의 효과는 이 실행으로 확인되지 않는다(일봉 실행기 = 하루 1회 시가 판정과 같음). ETHUSDT는 거래 규칙·유니버스가 없어 돌리지 않았다.

### 5.3 전략실 수치 — [전략실 보고] (운영 미재현, 과대 추정)
| 항목 | 값 | 출처 |
|---|---|---|
| 2종목 23회 일정 2017-09~2025 재생(research/crypto_trend_replay.py — 운영 도메인 객체로 1시간마다 evaluate, 다음 봉 시가 체결, **운영 엔진 아님**) | **105건·+6,964%·MDD 33.5%**(v2 재생도 105건·+6,963.7%·MDD 33.5% — v1과 같음), 학습 2017-09~2021 비율 3.28, 후기 2022~2025 1.33 | lab-handoff.md·lab-notes.md·strategy.md |
| 7창(2019~2025) 사전 판정표(재생, 한도 미적용) | C-2 87건(6창 76) 미달, C-3 슬리피지 0.2% +345 USDT/건 충족, C-4 ±20% 10변형 충족, 손실 창 1/7 | 같음 |
| 운영 `app.backtest_m2` 2종목 **일봉판**(전략실 two_coin_engine, 하루 1회 00:15) | 가정 한도(L1 2.75%) 55건·+2,840%·MDD 28.9%·거부 0 / PAPER 축소판 + A-064 55건·+13.5%·거부 0 | 같음 |
- 운영 공식 판정 창은 CP-037 D-222 **M2 6창(2020~2025)** — 전략실 7창과 다르다. 공식 판정표(C-1~C-8)·C-2 처리(CP-038)·L4 1%에서의 K2 영향(V-334)은 T-068 뒤.

## 6. 재현 뒤 적용할 전략실 "폐기 기준" (lab-notes.md 맨 위, 사전 등록 2026-10-18 01:39 — v2에 그대로 적용, 전략실 표시)
1. 엔진 7창(또는 운영 6창) **C-3**(슬리피지 0.2% 거래당 기대값 > 0) 또는 **C-4**(±20% 변형 모두 > 0) 미충족 → **폐기**.
2. 엔진 전체 구간(2017-09~2025) 거래 수가 전략실 재생 **105건과 ±15%**(약 89~121건) 넘게 다르거나 최종 수익이 **±30%** 넘게 다르면 → 원인 분석 전 **인계 보류**.
3. 운영 **L4 1%** 적용 시 거래 수가 재생 대비 **30% 넘게** 줄면 → "한도가 결과를 지배" 표시, L4 값은 사용자 결정으로 넘김.
- 시도 상한: 버전당 엔진 시도 5회(라벨별 기록). v2는 규칙·수치 무변경(일정 표현만)이라 전략실은 v1 근거 수치를 그대로 비교한다고 적었다 — 시도 횟수를 v1과 합산할지는 [미확인, 사용자·전략실 확인]. 이번 일봉 근사(§5.2)는 장중 엔진 시도가 아니므로 5회에 넣지 않는다 [제안].
- 주의: (2)의 "최종 수익 ±30%"는 +6,964% 기준이면 약 +4,875%~+9,053% — 비교 조건(한도·수수료·슬리피지·초기 금액)을 전략실 재생과 맞춰야 의미가 있다.

## 7. 검증 기록
- 시험 `tests/scenario/test_tm2_17_crypto_trend.py`(v2로 갱신): 파라미터 2개(period_minutes 60), 명세·능력, AST 입력, 분봉 무영향·일봉 필수, 등록(BACKTEST 통과·PAPER 거부·CAP-INS-01 거부), 실운영 등록 경로(BACKTEST 60분 격자·PAPER 거부), 조립(ETHUSDT 거부), F-1 해소(격자 60분 23회, 파일 2개), 격자 거부(eval_times·M1), F-2 해소(validate_deploy 통과·거부 6경우), 15분 격자 통과·배포 거부, F-3 xfail strict, 시작 규칙 현재 동작, 1분봉 CLI 실행·신호 0, 1분봉 입력 모양 신호 0, 규칙 동작 6개(PAPER 크기 4점 포함 9건). 가상 가격.
- 시험 변경 이유(구현 가이드 "테스트 수정 시 이유·영향 ID"): 전략실 v2(A-119 ①)로 F-1·F-2 동작이 바뀜 — v1 "현재 동작 기록" 시험 3개(격자 거부·23개 요구·1분봉 CLI 거부)를 v2 동작 시험으로 바꾸고, F-1·F-2 xfail strict 2개를 통과 시험으로 전환. v1에서 이 두 xfail은 기대한 이유로 실패했다(9fdb517 전체 실행 xfailed 3). 규칙 동작 기대값은 그대로.
- 실행(2026-10-18): `.venv\Scripts\python -m pytest -q tests/scenario/test_tm2_17_crypto_trend.py` → **24 passed, 1 xfailed** / `tests/architecture` → **27 passed** / 전체(`-m` 없음) → **3339 passed, 1 skipped, 1 xfailed, 실패 0** (20분 49초). 건너뜀 1 = 기존 test_t042_allocation.py:290, xfail 1 = F-3. (v1 때 3335 passed·3 xfailed → 이 파일 시험 수 23→25, xfail 2개 통과 전환)

## 8. 가정·미확인·차단
| 구분 | 항목 | 차단 대상 |
|---|---|---|
| 해소 | F-1·F-2(주기 일정 표현) | — (전략실 v2) |
| 차단 | 장중 엔진 (a) 여러 심볼 한 계좌·(b) 확정 일봉 입력·(d) 매분 L4 — A-119 ② T-068 범위 확장(CP 초안부터) | 재현·공식 판정표 |
| 차단 | F-3 공통 CP(A-119 ②) → 그 뒤 유니버스 ETHUSDT(A-119 ③) | M2 조립·PAPER |
| 차단 | M2 능력 "지원" 승격(T-M2-16·T-056 뒤) | PAPER 등록 |
| 미확인 | 전략실 1시간·일봉 봉(1분봉 "있는 분만" 묶음)과 운영 일봉(CAP-DAT-01) 일치 | 재현 차이 해석 |
| 미확인 | L1 ≥ 2.75%(잠정)·L4 영향(V-334) | LIVE 한도 — 제안만, 결정은 사용자 |
| 미확인 | 전략실 7e011c1 권장(v3 다종목 후보 1순위, v2는 T-068 뒤) — 운영 미검토 | 우선순위 결정(사용자) |
| 참고 | 저빈도 코인 전략 PAPER-1 최소 90일(A-119 ④, "저빈도" 정의는 CP) | PAPER-1 기간 |

## 9. 다음 단계 입력
- 운영: A-119 ② CP 초안(validate_rules 서명에 종목 + T-068 범위 확장) → 구현 뒤 §6 폐기 기준으로 재현 → 공식 판정표(M2 6창). F-3 반영 뒤 A-119 ③ 유니버스 변경.
- 사용자: 전략실 v3 권장과 v2 순서.

## 변경 이력
| 버전 | 날짜 | 변경 | 영향 ID |
|---|---|---|---|
| v0.1 | 2026-10-18 | 최초 작성(`review-v1.md`, v1 검토) | T-M2-17, A-118 |
| v0.2 | 2026-10-18 | v2 재검토(§0 추가, 복사·등록·결함·재현·검증 갱신), 파일 이름 `review-v2.md` | T-M2-17, A-119 ① |
