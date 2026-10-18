# crypto-trend 운영 검토 — v1(F-1~F-3) → v2(F-1·F-2 해소) → v3 재검토(F-3 전략 쪽 해소, 호출부는 T-070)

- 문서: `docs/strategies/crypto-trend/review-v3.md` (v0.1 `review-v1.md` 커밋 9fdb517 → v0.2 `review-v2.md` 커밋 b8fb28c — 이름을 바꿔 이어 씀) / 버전 v0.3 / 상태 **draft (미승인)** / 작성 2026-10-18 에이전트(T-M2-17 후보 검토, 브랜치 `feat/crypto-trend-v3-review` ← `feat/crypto-trend-v1-review` b8fb28c ← `feat/T-052-053-m2-periodic` 434211a)
- 근거 지시: A-118 A(v1), A-119 ①(v2), **A-123 ①**(2026-10-18, crypto-trend v3 후보 복사·운영 재검토 — **등록·PAPER 아님**). 승인 근거 CP-041(A-122 — D-270·D-271·V-340~V-342). 등록·PAPER·유니버스·한도·capabilities.md·`app/wiring.py`·`TESTNET_RULES`는 이 작업에서 바꾸지 않았다.
- **생존편향·사후 설계 포함 — 전략실 수치는 모두 과대 추정으로 읽는다**(BTC·ETH 사후 선택, 게이트·1시간 판정은 결과를 본 뒤 설계 — 전략실 자체 표시).
- 관련 ID: T-M2-17, A-118, A-119 ①②③, A-122, A-123 ①, CP-041 D-270·D-271·D-275·D-276·T-070·V-340~V-342, CP-037 D-219·D-221·D-222, CP-040 D-258·D-265·T-065~T-068, CP-036 D-208·D-209·DP-54, A-064·A-096.

## 0. v3 재검토 요약 (A-123 ①, 2026-10-18)

| 항목 | v2 (b8fb28c) | v3 (이번) |
|---|---|---|
| 전략실 원본 | 커밋 a5c1405 `CryptoTrendV2` | 작업 트리 파일(아래 sha256 4개 지정값과 일치) `CryptoTrendV3`. v2는 전략실 `strategy-v2.py`·`params-v2*.toml`로 보존 |
| 로직 | — | v2 대비 바뀐 곳: 머리 설명(v3 단락 3줄)·`VERSION` 2→3·`STEPS` 주석(ETH "가정" → "테스트넷 확인값")·클래스 이름·import 1줄(`trading_system.domain.trading_rules.SymbolRules`)·**`validate_symbol_rules` 추가**·`validate_rules` 독스트링. **`evaluate`·`validate_rules` 본문·`validate_deploy`·`_sig`·`build_spec`·지표 함수·`STEPS` 값 무변경**(diff 확인), 규칙 동작 시험 그대로 통과 |
| 파라미터 | 7개 | **값 같음** — 두 파일 모두 1행 주석과 `version = 2 → 3`만 다름(diff 확인) |
| F-1·F-2 | 해소 | 해소 유지(시험 그대로 통과) |
| F-3 종목별 규칙 검사 | 남음 | **전략 쪽 해소** — `validate_symbol_rules`가 D-270 형식으로 있고 BTC에 ETH 단위를 거부. **운영 호출부(`spot_rule_problems`)는 아직 `validate_rules`만 부름 → xfail strict 유지**, 해소는 CP-041 T-070 |
| 등록 BACKTEST / PAPER | 통과 / 거부 | 같음 |
| M2 조립 유니버스 | ETHUSDT 거부(A-096) | 같음(D-272 순서 — T-070·T-071 뒤 사용자 결정) |
| 재현 | 불가 | **불가** — 1분봉 실행기는 v3를 받아 끝까지 돌지만 신호 0(일봉 입력 없음·단일 심볼) |

## 1. 복사 — 출처·sha256 대조

전략실 원본(읽기만): `C:\AI\strategy-lab\candidates\crypto_trend\`. 지정값 4개와 원본 파일 sha256이 모두 일치한 뒤 복사했다(2026-10-18, `sha256sum`).

| 원본 | 지정 sha256 = 원본 실측 | 줄바꿈 | 운영 경로 | 복사본 sha256 | 판정 |
|---|---|---|---|---|---|
| `strategy.py` | `f6f23ef79b9883c64809d577c93c35e345ed8a31c415b0197e650b0b262c06f0` 일치 | CRLF 172줄, 단독 CR 0 | `src/trading_system/strategy/crypto_trend.py` | `f1bda84a3f31b11f5fb376778dee532205c3698c2d79c02bc7896b0212db5acf` | 원본을 CRLF→LF 변환한 바이트와 **동일**(단독 CR 0이므로 역변환하면 원본 값과 같음). 운영 수정 없음 |
| `params-v3.toml` | `c0f04c787ec404a3d72aba32f3c755908c1d8f4ca1ebe80345d2be9cef0764d4` 일치 | LF | `config/strategies/crypto-trend-v3.toml` | 원본과 같은 값 | **바이트 동일** |
| `params-v3-paper.toml` | `190ec8570cec76cd8afd76fff940514c424807dea83ba02f702c20513b124a9d` 일치 | LF | `config/strategies/crypto-trend-v3-paper.toml` | 원본과 같은 값 | **바이트 동일** |
| `strategy.md` | `0e43e848b9fe8367fe2bd133918ba393e147a069862c3c96176fde7bc4789fe9` 일치 | CRLF | `docs/strategies/crypto-trend/strategy.md` | `9d83ad26…`(머리 안내 포함) | 머리 안내 1단락을 빼면 LF 변환 원본(`329634b5…`)과 동일 |
| `handoff.md` | 지정값 없음 — 실측 `28b50191dac9a7fe…` | CRLF | `docs/strategies/crypto-trend/lab-handoff.md` | `223c9e1b…` | 머리 안내 1줄 빼면 LF 변환 원본(`f91cfb0f…`)과 동일. **지정값이 없어 대조 아님** |
| `notes.md` | 지정값 없음 — 실측 `08047f251f2be4f2…` | CRLF·LF 혼합 | `docs/strategies/crypto-trend/lab-notes.md` | `4fc602d6…` | 머리 안내 1줄 빼면 LF 변환 원본(`47998e2e…`)과 동일. **지정값이 없어 대조 아님** |

- 줄바꿈: 저장소 `.gitattributes`(`* text=auto eol=lf`, 03-design D-063)가 LF를 요구하고 v1·v2 사본도 LF였다(v2는 전략실 커밋 내용, LF). 원본 작업 트리 파일은 CRLF라 **LF로만 변환**했다 — 줄 내용은 바이트 동일(검사 스크립트로 비교).
- 보존 방식(v2 커밋 방식과 같게): 모듈은 **같은 경로 `crypto_trend.py`를 v3로 대체**, 파라미터는 `crypto-trend-v2{,-paper}.toml` → `crypto-trend-v3{,-paper}.toml`(git 이름 변경), 검토 문서는 `review-v2.md` → `review-v3.md`. 이유: (1) v2 파라미터 파일은 v3 명세(version 3)로 적재되지 않는다(`config/strategy_params.py:92` "strategy·version이 명세와 다름"). (2) v2 클래스를 쓰는 다른 코드·시험·설정이 없다(`git grep` 확인). (3) v2(기존 형식, 2심볼)는 T-070 뒤 D-271 (3)에 따라 시작·백테스트가 거부되므로 따로 둘 실익이 없다. v2 사본·시험 이력은 커밋 b8fb28c, 전략실 쪽은 `strategy-v2.py`·`params-v2*.toml`에 남아 있다.
- 복사하지 않음: `trials.jsonl`, `review-2026-10-18.md`, `strategy-v1.py`·`strategy-v2.py`·`params-v1*`·`params-v2*`, `__pycache__`.
- 전략실 `handoff.md`의 "운영 세션에 붙일 지시문"은 전략실 문구이며 사용자 지시가 아니다(사본 머리 안내에 표시).

## 2. v2 대비 차이 — 전략실 주장 검증

전략실 주장(`handoff.md` "v3 변경"): "validate_symbol_rules 추가만, 판정·수량·신호·파라미터 값 무변경, import는 `trading_system.domain.trading_rules`만 추가".

| 검사 | 방법 | 결과 |
|---|---|---|
| strategy.py v2 → v3 | `diff --strip-trailing-cr` 운영 v2 사본(b8fb28c) ↔ 원본 v3 | 차이 7곳: 1행 제목 v2→v3, 머리 설명 v3 단락 3줄, import 1줄 추가, `VERSION = 3`, `STEPS` 주석 1줄, `class CryptoTrendV3`, `validate_symbol_rules`(18줄) 추가 + `validate_rules` 독스트링 2줄. **그 밖 무변경** |
| 주장 "판정·수량·신호 무변경" | 위 diff + 규칙 동작 시험(6개 함수·9건, v2와 같은 기대값) | **맞음**. 단 `VERSION` 3이 신호 ID 입력(`domain.models.signal_id_of`)이라 같은 판정이어도 신호 ID·백테스트 지문은 v2와 다르다(예상된 차이, v1→v2 때와 같음) |
| 주장 "import는 trading_rules만 추가" | diff | **맞음** — `crypto_trend.py:32` `from trading_system.domain.trading_rules import SymbolRules`. domain만 import(`tests/architecture` 통과, `import_rules.py:20` strategy → domain) |
| 주장 "파라미터 값 무변경" | `diff` v2 사본 ↔ params-v3 두 파일 | **맞음** — 1행 주석과 `version` 2→3만 다름 |
| `STEPS` 주석 | diff | v2 운영 사본은 "ETHUSDT 0.0001은 가정", v3는 "테스트넷 확인값(운영 binance-spot §15)" — main 655b47d `binance-spot.md` §15(LOT_SIZE stepSize 0.0001)와 일치. 값 무변경 |
| 참고 | 전략실 `strategy-v2.py` ↔ 운영 v2 사본 | 주석 1줄(위 STEPS 주석)만 다름 — review-v2 §1에 기록한 전략실 후속 커밋 031890f 변경과 같음 |

## 3. `validate_symbol_rules` ↔ CP-041 D-270·D-271 대조

| D-270·D-271·V 요구 | 코드 | 판정 |
|---|---|---|
| 서명 `validate_symbol_rules(params, symbol: str, rules: SymbolRules) -> list[str]`, 정적 메서드 허용 | `crypto_trend.py:128-129` `@staticmethod def validate_symbol_rules(params: ParamValues, symbol: str, rules: SymbolRules) -> list[str]` | 일치 |
| `SymbolRules` = `domain.trading_rules`의 불변 자료형(전략 구조 규칙 지킴) | `domain/trading_rules.py:42-58` `@dataclass(frozen=True) class SymbolRules` — 필드 `instrument`(:46)·`lot_step`(:52)·`market_step`(:55, "0이면 LOT_SIZE stepSize", D-208). 코드가 쓰는 필드 3개 모두 같은 이름 | 일치 |
| 반환 = 문제 문자열 목록, 빈 목록 = 통과 | `:134-144` | 일치. 모든 문제 문자열이 `"{심볼}: "`로 시작 — V-340 "심볼 이름이 든 문제 각 1건" 충족(시험) |
| 거부 ① STEPS 밖 심볼 | `:134-136` `STEPS.get(symbol) is None` → 1건 반환(나머지 검사 생략) | 동작 확인(SOLUSDT). D-270 본문에는 없는 전략 자체 방어 — D-271 (1) 범위(U-A ∩ 설정 유니버스)에서는 호출되지 않는 경로 |
| 거부 ② `rules.instrument ≠ symbol` | `:138-139` | 동작 확인(BTCUSDC 규칙, ETH 규칙을 BTC로) |
| 거부 ③ `STEPS[심볼] ≠ rules.lot_step` | `:140-141` | 동작 확인(BTC↔ETH 단위 뒤바꿈 — F-3의 바로 그 경우) |
| 거부 ④ `market_step > 0`이고 전략 단위가 그 배수 아님 | `:142-143` `step % rules.market_step != 0` | 동작 확인(0·같은 값·약수 통과, 0.00002·0.001·0.0003 거부). D-208(0 = LOT_SIZE와 같음)과 맞다 |
| D-271 (2) 기존 `validate_rules` 유지(둘 다 있으면 새 형식만 호출 — V-341) | `:146-153` 본문 v2와 같음, 독스트링만 변경 | 유지 확인. "새 형식만 호출"은 **호출부 동작이라 T-070 몫** — 현재 `app/wiring.py:303` `getattr(si.strategy, "validate_rules", None)`만 부름(시험으로 현재 동작 기록) |
| D-270 `TESTNET_RULES`에 ETHUSDT 추가·`PAIRS` 추가 | 이 브랜치 `app/backtest_m2_cli.py:49-57`은 BTCUSDT만 | **T-070 몫 — 미반영(이 작업 범위 밖)**. 시험은 §15 값으로 ETH 규칙을 시험 안에서만 만든다 |
| D-270 범위 밖 | `min_notional`·`lot_min_qty`·`tick_size`는 검사하지 않음 | D-270이 요구하지 않음. 주문별 필터는 의도 생성·게이트의 `filter_violation`(`trading_rules.py:83`)이 따로 검사 |

## 4. 구조·능력·M2 등록 가능 여부 (v3에서 다시 확인 — v2와 같음)

| 항목 | v3 결과 | 근거(시험, `tests/scenario/test_tm2_17_crypto_trend.py`) |
|---|---|---|
| 능력 | CAP-TRG-02·CAP-DAT-01·CAP-ORD-01·CAP-INS-05 (CAP-INS-01·CAP-TRG-01·CAP-DAT-02·CAP-HLD-02 없음) | `test_spec_rules_priorities_and_capabilities` |
| 입력(AST) | evaluate가 읽는 종목 속성 = `instrument·data_ok·has_pending_order·daily_bars·last_price·position·statuses` | `test_code_reads_only_daily_bars_last_price_and_state_inputs` |
| 등록 검사 BACKTEST | **통과**(능력 4개 "검증 중") | `test_registration_backtest_passes_with_trg02_verifying_and_paper_refused` |
| 등록 검사 PAPER | **거부**(능력 4개 "검증 중 — PAPER 등록 불가") | 같은 시험 |
| 실운영 등록 경로 `_register_strategies` | BACKTEST 임시 DB: 통과, `LoadedStrategy.period` 60분·`eval_times ()` / PAPER 임시 DB: "전략 등록 거부" | `test_operational_registration_path_builds_60min_grid_in_backtest` (시험 임시 폴더 — 운영 DB·`var/` 미사용) |
| 주기 격자·validate_deploy | 60분 23회 통과 / 23개·15분·M1 거부 | F-1·F-2 해소 시험 그대로 |
| M2 조립 유니버스 | ETHUSDT 거부(A-096) | `test_m2_assembly_refuses_ethusdt_outside_universe_a096` |
| 시작 규칙 검사(현행 호출부) | BTCUSDT 실측 통과, BTC에 ETH 단위도 통과(기존 형식) | `test_start_rule_check_current_behaviour`, `test_operational_start_check_still_uses_legacy_form_until_t070`, F-3 xfail |
- 결론: **v3는 BACKTEST 등록과 실운영 등록 경로를 통과하고 PAPER는 거부된다**(v2와 같음). 실운영 M2 조립은 T-070(호출부 전환·ETH 규칙) → T-071 → 유니버스 변경(D-272, 사용자) 전까지 불가. PAPER는 능력 "지원" 승격 전까지 불가.

## 5. 결함·관찰 (전략 로직은 운영에서 고치지 않음)

| ID | 내용 | v3 상태 | 처리 |
|---|---|---|---|
| F-1 | `period_minutes` 없음 | 해소(v2) | — |
| F-2 | validate_deploy ↔ D-219 | 해소(v2) | — |
| **F-3** | 종목 없는 `validate_rules` → BTC에 ETH 단위 0.0001이 와도 통과 | **전략 쪽 해소**(v3 `validate_symbol_rules`가 거부), **호출부 남음** | xfail strict 유지 — 이유 문구를 "호출부가 아직 validate_rules만 부름(T-070)"으로 갱신. T-070 구현 뒤 통과 시험으로 전환 |
| O-1~O-4 | 일봉 실행기 시가 판정, 1분봉 실행기 단일 심볼·`daily_bars ()`, 실운영 스냅샷 입력 모양, 손절 정의·동시 신호 순서 | review-v2 §4와 같음 | T-068 |
| O-5 | 전략실 `handoff.md` 3행이 "T-071(1분봉 실행기 여러 심볼·확정 일봉)"이라고 적음 — CP-041에서 T-071은 `universe-add` 도구이고, 실행기 확장은 **T-068 개정분(D-275·D-276)** | 문서 표기 차이(코드 영향 없음) | 전략실에 알림 [제안] |
- 새 결함(F-4 이후)은 찾지 못했다.

## 6. 재현 가능 여부 — **불가** (v2와 같음)

- 1분봉 실행기(`app.backtest_m2_minute`, T-053)는 v3를 받아 끝까지 실행한다(가상 상승 1분봉 2일: 판정 46회, 신호 0, `summary.json` version 3) — 일봉 입력이 없고(`daily_bars ()`) 단일 심볼이라 **재현 아님**. 일봉 실행기(`app.backtest_m2`)는 validate_deploy가 판정 시각 23개를 거부한다(v2와 같음, review-v2 §5.2).
- 충실한 재현에 필요한 것: 여러 심볼 한 계좌(D-275), 판정 시각 기준 확정 UTC 일봉 입력(D-276), 60분 판정 `last_price`, 매분 저가 L4·K2(D-265). 현재 어느 실행기에도 없다.
- 선행 작업(CP-041 §3 순서 문구 기준): T-052·T-053 병합 → **T-070**(호출부 `validate_symbol_rules`·`TESTNET_RULES`/`PAIRS` ETHUSDT — T-068 개정분의 백테스트 CLI가 새 형식을 씀) → CP-040 **T-065 / T-066 → T-067 → T-068**(+ CP-041 D-275·D-276 확장) → 재현 → §7 폐기 기준 → 공식 판정표(D-222 M2 6창).
- 일봉 근사 같은 대체 계산은 이번에 하지 않았고, 공식 결과로 쓰지 않는다. v3는 판정 로직이 v2와 같아 v2 일봉 근사(review-v2 §5.2, +7.78%·왕복 27)를 다시 돌릴 이유도 없다.
- 전략실 수치(재생 105건·+6,964%·MDD 33.5%, v3 재생도 같다고 전략실 표시)는 [전략실 보고]이며 운영 미재현이다 — 표는 review-v2 §5.3(커밋 b8fb28c)과 같다.

## 7. 재현 뒤 적용할 전략실 "폐기 기준" (lab-notes.md 맨 위, 사전 등록 2026-10-18 01:39)
1. 엔진 7창(또는 운영 6창) C-3 또는 C-4 미충족 → 폐기.
2. 엔진 전체 구간 거래 수가 재생 105건과 ±15% 넘게, 또는 최종 수익이 ±30% 넘게 다르면 → 원인 분석 전 인계 보류.
3. 운영 L4 1% 적용 시 거래 수가 재생 대비 30% 넘게 줄면 → "한도가 결과를 지배" 표시, L4 값은 사용자 결정.
- 시도 상한(버전당 엔진 시도 5회)을 v1·v2·v3 합산할지는 [미확인, 사용자·전략실 확인].

## 8. 검증 기록
- 시험 `tests/scenario/test_tm2_17_crypto_trend.py`(v3로 갱신): 기존 25개(v2) 그대로(클래스·파일 이름·버전 기대값만 v3로) + 새 15개 — 두 형식 보유(D-271 (2)), 실측 규칙 통과(파일 2개), 단위 뒤바꿈 거부(심볼 이름 포함, 기존 형식은 통과함을 대조), STEPS 밖 심볼 거부, 규칙 심볼 이름 다름 거부(1건·2건), MARKET step 배수 8경우, 현행 호출부가 기존 형식만 부름. 가상 가격, ETH 규칙은 BN §15 값(MARKET maxQty·maxNotional은 §15에 없어 BTC 값을 빌린 가상값 — 검사에 쓰이지 않음).
- 시험 변경 이유: 전략실 v3(A-123 ①) — 클래스 이름·버전·파일 이름 변경 반영, F-3 xfail 이유 문구 갱신(전략 쪽 해소, 호출부 T-070), D-270 검사 시험 추가. 기존 기대값(규칙 동작·등록·격자·validate_deploy) 무변경.
- 실행(2026-10-18, 작업 트리 `.claude/worktrees/agent-abe1df022c93912b3`, 저장소 `.venv` 파이썬):
  - `..\..\..\.venv\Scripts\python -m pytest -q tests/scenario/test_tm2_17_crypto_trend.py` → **39 passed, 1 xfailed** (xfail = F-3)
  - `..\..\..\.venv\Scripts\python -m pytest -q tests/architecture` → **27 passed**
  - 전체 시험 스위트: **미실행**(지시 범위 밖).
- `app/wiring.py`·`backtest_m2_cli.py`는 바꾸지 않았다(T-070 몫).

## 9. 가정·미확인·차단
| 구분 | 항목 | 차단 대상 |
|---|---|---|
| 차단 | T-070(D-270 호출부·D-271·`TESTNET_RULES` ETH) — 그 전에는 운영 시작 검사가 종목별로 검사하지 않음(F-3 호출부) | M2 조립·여러 심볼 백테스트 CLI |
| 차단 | 장중 엔진 여러 심볼 한 계좌·확정 일봉·매분 L4(T-068 + D-275·D-276) | 재현·공식 판정표 |
| 차단 | 유니버스 ETHUSDT(D-272, T-070·T-071 뒤 사용자) | M2 조립 |
| 차단 | M2 능력 "지원" 승격 | PAPER 등록 |
| 미확인 | 전략실 `handoff.md`·`notes.md`는 지정 sha256이 없어 대조하지 못함(복사본은 실측값 기록) | 문서 사본 신뢰 — 코드·파라미터에는 영향 없음 |
| 미확인 | 전략실 작업 트리 파일이 전략실 커밋에 들어 있는지(커밋 해시) — 이 작업은 작업 트리 파일 sha256으로만 대조 | 출처 기록 |
| 미확인 | 전략실 1시간·일봉 봉과 운영 일봉 일치, L1·L4 영향(V-334) | 재현 차이 해석·LIVE 한도 |
| 미확인 | 시도 횟수 합산 방식 | 폐기 기준 적용 |

## 10. 다음 단계 입력
- 운영: T-070 착수 지시 시 이 브랜치의 F-3 xfail을 통과 시험으로 전환하고, D-271 (2)(두 형식 → 새 형식만)·V-341을 v3로 확인. 그 뒤 CP-040 T-065~T-068(+D-275·D-276) → 재현.
- 사용자: v3 사본 채택(이 브랜치 병합) 여부, 시도 횟수 합산 방식.
- 전략실: O-5 표기 정정 [제안].

## 변경 이력
| 버전 | 날짜 | 변경 | 영향 ID |
|---|---|---|---|
| v0.1 | 2026-10-18 | 최초 작성(`review-v1.md`, v1 검토) | T-M2-17, A-118 |
| v0.2 | 2026-10-18 | v2 재검토, 파일 이름 `review-v2.md` | T-M2-17, A-119 ① |
| v0.3 | 2026-10-18 | v3 재검토(복사·sha256 대조, v2 대비 diff, D-270 대조, 시험 15개 추가, F-3 전략 쪽 해소·호출부 T-070), 파일 이름 `review-v3.md`. v2 본문(§2 구조 표·§5 재현 표·§5.2 일봉 근사·§5.3 전략실 수치)은 커밋 b8fb28c 참조 | T-M2-17, A-123 ①, CP-041 D-270·D-271 |
