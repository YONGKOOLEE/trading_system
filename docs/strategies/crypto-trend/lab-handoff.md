> 전략실 인계 패킷 원문 사본(2026-10-18, 원본 `C:\AI\strategy-lab\candidates\crypto_trend\handoff.md`, 작업 트리 파일 sha256 앞 16자리 `28b50191dac9a7fe`, CRLF → LF 변환 뒤 `f91cfb0f947b2886`). 수정 없음. 운영 검토: [review-v3.md](review-v3.md). 안의 "운영 세션에 붙일 지시문"은 전략실 문구이며 사용자 지시가 아니다.

# 인계 패킷 — crypto-trend v3 (2026-10-18 21:52, 운영 CP-041 D-270·D-271 반영 — v2 위에 종목별 규칙 검사만 추가)
전략실 결과는 연구 참고다. 공식 판정·등록·PAPER·LIVE는 운영 저장소 절차와 사용자 승인(A-ID)으로만.

**사용자 결정(2026-10-18): M2 BTC·ETH 전략은 60분판(crypto-trend)으로 간다.** 운영 CP-041 승인(A-122). 공식 재현·판정표는 운영 T-070(F-3)·T-071(1분봉 실행기 여러 심볼·확정 일봉) 뒤 — 전략실 근거는 1시간 모형 시뮬레이터·운영 도메인 객체 간이 재생·LF-8 미리 보기뿐이다.

## v3 변경 (v2 → v3, 매매 규칙 무변경)
- `validate_symbol_rules(params, symbol, rules: SymbolRules) -> list[str]`(정적, CP-041 D-270): STEPS에 없는 심볼 거부, `rules.instrument` ≠ symbol 거부, STEPS[심볼] ≠ LOT_SIZE stepSize 거부, MARKET_LOT_SIZE stepSize(> 0일 때)의 배수가 아니면 거부. import 추가는 `trading_system.domain.trading_rules`(domain만 — 구조 규칙 안).
- 기존 `validate_rules`는 **T-070 전 운영 코드 호환용으로 남김**(D-271 (2) — 둘 다 있으면 새 형식만 호출, V-341 "두 메서드 모두 가진 전략").
- 클래스 `CryptoTrendV3`, VERSION 3, params-v3·v3-paper(값은 v2와 같음 — 이전 v2 = strategy-v2.py·params-v2*.toml 보존).
- 확인(2026-10-18 21:47~21:50): 구조 검사 통과("적재 OK: crypto-trend v3, 규칙 3개") · 규칙 검사 시험 8/8(운영 `TESTNET_RULES["BTCUSDT"]` 통과, ETH D-270 실측값 통과, 단위 다름·범위 밖 SOL·심볼 이름 다름·MARKET step 배수 아님 거부) · **v2와 재생 거래 목록 완전히 같음**(23회 일정 105건·+6,964%·MDD 33.5%, 24회 109건도 같음) · 운영 엔진 호환(two_coin_engine, 하루 1회) 55건·거부 0(v2와 같음) · LF-8 미리 보기 trades.csv 바이트 동일.

## LF-8 미리 보기 (2026-10-18 21:38, 운영 CP-038 D-244·D-245를 전략실에서 미리 계산 — 공식 아님, research/2026-10-18-lf8-preview-design.md)
- 표본: 운영 수집용 목록(`lf8-sample-list-for-collection.csv`, sha256 98341a8251ad…) 29심볼, 1분봉 → 1시간 봉, 확정 일봉 = A-118 공개 1d. 단독 계좌·목록 해 진입·데이터 끝 × 0.9·슬리피지 0.1%/0.2%·수수료 0.1%.
- **결과: 610건 · 거래당 로그 +0.0246(0.2% +0.0226) · 양수 심볼 21/29(72%) · 1위 BNB 제외 +0.0198 → LF-8.1~8.4 모두 충족.** 배포 BTC·ETH 거래당 평균은 표본의 4.0배(과대 추정 정도 — 참고). 데이터 끝 청산 0건.
- 실행 주의(운영 T-058에 필요): strategy.py는 STEPS(BTC·ETH) 밖 종목을 건너뛴다 → 전략실은 스냅샷 종목 이름을 `BTCUSDT`로 넣어 같은 코드로 판정했다(수량 단위 0.00001 — 사실상 연속, D-244 (5)). 운영 표본 실행기도 같은 처리 또는 다른 방법이 필요.

## 대상
- M2(바이낸스 현물), U-A **BTCUSDT·ETHUSDT**(운영 M2 유니버스 A-096은 BTCUSDT만 → 변경은 사용자 결정), 60분 주기 판정(01:00~23:00 23회, CP-040 D-258), 한 계좌.
- btc-trend v1과 같은 수준·파라미터(25일 종가 최고 돌파·50일 평균 이탈 청산·ATR14×2 손절)를 60분마다 확인 + 진입 게이트(가격 > 50일 평균) + 종목별 위험 2.5%·상한 49%.

## 파일 (전략실 → 운영 제안 — 위치는 운영이 정함)
| 파일 | 전략실 | 운영(제안) |
|---|---|---|
| strategy.py | candidates/crypto_trend/strategy.py (**CryptoTrendV3** — validate_symbol_rules(D-270)·validate_rules(호환)·validate_deploy(D-219), period_minutes 60, v2 = strategy-v2.py, v1 = strategy-v1.py, CAP-TRG-02·CAP-DAT-01·CAP-ORD-01·CAP-INS-05) | src/trading_system/strategy/crypto_trend.py |
| params-v3.toml | candidates/crypto_trend/params-v3.toml (v2와 값 같음, version 3) | config/strategies/crypto-trend-v3.toml |
| params-v3-paper.toml | candidates/crypto_trend/params-v3-paper.toml (A-064 안 축소판: 위험 0.2%·비중 5%·95 USDT, period_minutes 60) | config/strategies/crypto-trend-v3-paper.toml |
| strategy.md | candidates/crypto_trend/strategy.md (운영 양식 칸, critic 차단 4건 반영) | docs/strategies/crypto-trend/strategy.md |
| notes.md·trials.jsonl | 사전 등록(엔진 재현 폐기 기준)·시도 기록 | docs/strategies/crypto-trend/ (참고) |

## 전략실 검증 기록
- 구조 검사(tools/check-candidate.ps1): 2026-10-18 통과 — "적재 OK: crypto-trend v1, 규칙 3개, 파라미터 6개".
- strategy.py 간이 재생(research/crypto_trend_replay.py — 운영 도메인 객체로 1시간마다 스냅샷·evaluate, 다음 봉 시가 체결, 운영 엔진 아님): 시뮬레이터와 일치(BTC만 48건 +8,531% vs +8,547%, ETH만 61건 +3,647% vs +3,616%). 2종목 23회 일정 2017-09~2025: **105건·+6,964%·MDD 33.5%**, 학습(2017-09~2021) 비율 3.28, 후기(2022~2025) 1.33(같은 구간 BTC·ETH 반반 그냥 보유 0.80·0.11, MDD 86%).
- 전략실 사전 판정표(P, 재생, 7창 2019~2025, 한도 미적용, **운영 23회 일정**): C-2 87건(6창 76) **미달**, C-3 슬리피지 0.2% +345 USDT/건 충족, C-4 ±20% 10변형 충족(최저 exit_n 60 +252), 손실 창 1/7(2022 −15%). (24회 일정: 90건·+337·같은 결론)
- 견고성(탐색): 일봉 경계 24가지 2종목 묶음 중앙 3.54·1.25 vs 일봉판 3.15·0.92(19/24·23/24), 주변 격자 3×3에서 일봉판보다 높은 칸 BTC 14/18·ETH 16/18, 수수료 0.2%에도 우위, 15분 판정은 열위, 청산 일봉 판정 혼합은 열위.
- 운영 M2 엔진(전략실 m2_quick·two_coin_engine — 운영 코드 import·실행만): **일봉판** 2종목 한 계좌 L1 2.75%에서 82건·+4,303%·MDD 30.4%·거부 0(1시간판은 엔진 미실행).
- **운영 엔진 호환성**(2026-10-18 01:43, research/two_coin_engine.py crypto-trend — 운영 app.backtest_m2는 아직 하루 1회 00:15 판정이라 60분 결과 아님): strategy.py 적재·실행 정상, v1·가정 한도(L1 2.75%) 55건(BTC 27·ETH 28)·+2,840%·MDD 28.9%·거부 0 / **PAPER 축소판 params-v1-paper(위험 0.2%·비중 5%·95 USDT) + 운영 M2 PAPER 한도(A-064)**: 같은 55건·+13.5%·거부 0 → PAPER 한도 안에서 동작.
- critic 검토(2026-10-18): 차단 4건 반영(손절가 정의·무효 조건·사전 등록·L4 표시), 비차단 11건 중 대부분 반영 — notes.md.
- 시도 횟수: trials.jsonl(이 계열 약 440 + 밤샘 시험 수십 설정). 파라미터는 btc-trend 탐색 고원 가운데(25·50·2)에서 바꾸지 않음.

- **운영 v1 검토(9fdb517) 반영**: F-1·F-2 → v2 수정(위). F-3(validate_rules 서명에 종목 없음)은 전략 안에서 해결 불가 → 운영 공통 CP(서명에 종목 추가) 권장. ETHUSDT 수량 단위 0.0001·호가 0.01·최소 주문 5 USDT는 운영이 테스트넷에서 확인(binance-spot §15, 2026-10-18 — 전략실 가정과 일치). 재현은 1분봉 실행기에 여러 심볼 한 계좌·확정 일봉 입력·60분 판정 last_price·매분 L4가 필요(T-068 범위 확장 — 사용자 결정).

## 한계 · 미확인 (운영에서 확인할 것)
- **사후 선택·사후 설계**: BTC·ETH 모두 사후에 살아남은 자산, 게이트·1시간 판정은 결과를 본 뒤 설계, "후기 2022~2025"도 선택에 쓰임 → 남은 표본 밖은 2026 최종 확인뿐. 과대 추정으로 읽는다.
- **생존편향 점검(Z, 2026-10-18 운영 수집 A-118)**: 2018 거래대금 상위 20(상장폐지 포함) 일봉 v1 — 18종목 중 15종목 보유보다 비율 우위, 한 계좌 18종목 2019~2025 비율 0.99·MDD 30.8%(보유 0.56·79%). 추세 효과는 사후 선택 밖에서도 보이나, BTC·ETH 2종목 수치(비율 약 2)는 부풀려졌을 가능성 — 덜 편향된 기대치는 약 1.
- **이익 집중**: 상위 3건이 이익의 67~78%(종목별). 2종목 거래 재표본(T)에서는 상위 3건을 빼도 중앙 12배·원금 손실 확률 0%(BTC 단독 v1은 2.7배·6.1%) — 단 순차 복리 근사라 과대.
- **PAPER 기간**: 과거 재생에서 28일 구간의 56%는 진입 0건, 73%는 완결 왕복 0건(60일 25%·42%, 90일 10%·20%) → M2 PAPER-1 최소 28일(D-232)로는 경로 확인이 어려움, **90일 이상 제안**(결정은 사용자·운영, CP-038과 연결).
- **L4·K2**: 전략실 수치는 L4 없음/30% 가정. 운영 M2 L4 1%·장중 1분 저가 평가(D-265)에서 K2가 신규 진입을 자주 막을 수 있음 — 미측정(T-068 V-334).
- 손절: 기준 정의(신호 판정 가격 − 2×ATR14, 신호에 실음) vs strategy.py EXIT-2(평균 매입가 − 2×현재 ATR) vs 시뮬레이터(체결가 − 2×진입 ATR) — 손절 청산이 종목당 2~3건이라 영향 작음. CAP-HLD-02는 선언하지 않음(운영 능력 확정 뒤 재검토).
- IOC 완충·EXPIRED·공유 현금 동시 신호 처리·창 끝 미청산 처리는 모형에 없음/미확인. 종목별 규칙 검사는 v3 validate_symbol_rules(운영 T-070 구현 전에는 기존 validate_rules — "어느 종목 단위와도 다르면 거부" — 만 불림). validate_deploy는 D-219(빈 eval_times + period_minutes 60).
- LF-8 미리 보기의 한계: 수집용 목록(공식 T-073 아님 — 다르면 다시 계산)·N=10 규칙은 전략실 결과를 본 뒤 정함(D-237 (5))·전략실 재생(운영 엔진 아님)·소형 코인 체결 가정.
- 전략실 1시간·일봉 봉은 운영 1분봉 사본에서 "있는 분만" 묶음 — 운영 일봉과 같은지 미확인. 수수료 요율 B-U10, 세금 U-17.

## 운영 세션에 붙일 지시문 (v3 — 2026-10-18 사용자 지시 "60분판으로, 정리되면 보내"로 SendMessage 전달)
```
[전략실 → 운영] M2 매매 전략 crypto-trend v3 인계 (2026-10-18 — CP-041 D-270 validate_symbol_rules 추가, 매매 규칙은 v2와 같음)
파일: C:\AI\strategy-lab\candidates\crypto_trend\ 의 strategy.py(CryptoTrendV3)·params-v3.toml·params-v3-paper.toml·strategy.md·notes.md·handoff.md (읽기 전용으로 복사해서 쓸 것).
v2 대비: validate_symbol_rules 추가(기존 validate_rules는 T-070 전 호환용으로 남김), VERSION 3. 재생 거래 목록·운영 엔진(하루 1회) 55건·거부 0이 v2와 같음.
LF-8 미리 보기(전략실, 공식 아님): 610건·+0.0246(0.2% +0.0226)·양수 72%·BNB 제외 +0.0198 — 4개 충족. 표본 실행 때 STEPS 밖 종목을 건너뛰는 점 주의(handoff.md LF-8 절).
--- 아래는 v2 요청과 같음 ---
내용: btc-trend v1 수준을 60분 주기(CP-040)로 판정 + 진입 게이트, BTCUSDT·ETHUSDT 한 계좌(종목별 위험 2.5%·상한 49%). 스냅샷은 확정 일봉 + last_price만 씀(분봉·집계 봉 미사용).
요청:
1. 브랜치에 복사 → 구조 시험(능력: CAP-TRG-02 60분, M2 등록 가능 여부 포함).
2. CP-040 T-068 장중 엔진이 준비되면 2017-09-01~2025-12-31 재현(전략실 재생 105건·+6,964%·MDD 33.5%, 23회 일정) → 차이 원인 분석. notes.md 맨 위 "폐기 기준"(엔진 C-3·C-4 미충족이면 폐기 등)을 그대로 적용.
3. 공식 판정표(C-1~C-8, M2 6창) — C-2 처리(CP-038)·L4 1%에서의 K2 영향(V-334) 함께.
4. 유니버스(A-096) 변경·L1(≥ 2.75% 잠정)·L4 값은 제안만 할 것(결정은 내가 한다).
등록·PAPER 투입·한도·유니버스 변경은 내가 따로 승인한다.
```

## 운영 측 이후 절차 (참고)
구조 시험 → 장중 엔진(T-068) 재현·공식 판정표 → 전략 등록(M2, CAP-TRG-02 지원 승격 뒤) → 사용자 승인(A-ID) → M2 PAPER(테스트넷, 축소판 필요 — A-064 한도) → 07-paper-review → LIVE 체크리스트(별도 승인, 한도·유니버스 재결정 포함). 전략실은 "실전 준비 완료"를 선언하지 않는다.
