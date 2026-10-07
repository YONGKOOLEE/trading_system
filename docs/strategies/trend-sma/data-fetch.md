# trend-sma 백테스트용 일봉 수집 안내 (T-018)

- 문서: data-fetch.md v0.1, 상태 draft (2026-10-07, 에이전트 작성 — 도구는 **미실행**, 가짜 응답 시험만 통과)
- 근거 승인: A-073 (E) "키움 모의 ka10081 조회만, 오늘 저녁 PAPER run 종료 뒤에만 실행", (F) "코스피 시가총액 상위 20~30개(ETF·우선주 제외)"
- 도구: `scripts/fetch-daily.py` → `src/trading_system/app/daily_fetch.py`, 시험 `tests/scenario/test_t018_daily_fetch.py`
- 주문 API: 호출 경로 없음(조회 `query_raw`와 토큰 폐기만). 혹시 시도가 생겨도 GuardedHttp가 전송 전에 막고 결과를 실패로 표시한다.

## 1. 하는 일
1. 연결 전 검사: `--mode PAPER`, PAPER run 실행 잠금, 하트비트(30초 이내면 거부), `TRADE_MODE=PAPER`·`.env.paper`·`connection_approved = true`(시작 점검과 같은 조건).
2. 종목 선정: ka10099 코스피 목록 → 제외(ETF·ETN 목록에 있는 코드, ETF·ETN·ELW 표시, 우선주, 거래정지, 투자유의·관리) → 남은 종목마다 ka10001 시가총액 → 상위 N(기본 30).
3. 종목별 ka10081 수정주가 일봉(2016-01-01부터 마지막 확정 거래일까지) → `<저장 폴더>\<코드>.csv` (`date,open,high,low,close,volume`, 기존 `kiwoom-ka10081-adj-20261002`와 같은 형식).
4. 끝나면 토큰 폐기(au10002).

## 2. 필드 확인 상태
| 용도 | API·필드 | 상태 | 근거 |
|---|---|---|---|
| 종목 목록·상태 | ka10099 `code`·`name`·`state`·`auditInfo`·`orderWarning`·`marketName` | 확인(실측) | kiwoom-kr.md §7b |
| ETF·ETN 구분 | ka10099 mrkt_tp 8·60 목록의 코드 | 확인(실측: 4개 시장 조회 정상) | kiwoom-kr.md §7b |
| 시가총액(순위) | ka10001 **`mac`** | **미확인 — 첫 실행 결과로 확인**. 없거나 해석 불가면 추측 없이 중단 | kiwoom-kr.md에 기록 없음 |
| 교차 확인 | ka10001 `flo_stk` × `cur_prc` | `cur_prc` 확인(사용 중), `flo_stk` 미확인. 화면에 "순위 일치 n"으로만 표시 | — |
| 상장주식수·가격(ka10099) | — | 기록 없음 → 쓰지 않음 | — |
| 일봉 | ka10081 `dt`·`open_pric`·`high_pric`·`low_pric`·`cur_prc`·`trde_qty`, `upd_stkpc_tp=1` | 확인(실측, 기존 수집·시작 점검) | kiwoom-kr.md §6·§7b |

- 시가총액 단위는 미확인이지만 같은 필드끼리 비교하는 순위에는 영향이 없다.
- 우선주 판정: 이름 끝 "우"·"우B" 등 또는 **코드 끝자리가 0이 아님(KRX 관례 — 가정)**. 잘못 빠진 종목이 있으면 `meta\excluded.csv`에 사유와 함께 남는다.

## 3. 실행 (사용자, 저녁)
조건: 오늘 PAPER run을 **Ctrl+C로 끄고 30초 넘게 기다린 뒤**. 일봉 확정 시각이 20:00이라(DP-5) **20:00 이후**에 실행해야 오늘 봉까지 들어간다(이전이면 전 거래일까지, 폴더 이름도 그 날짜).

PowerShell 창에서 한 줄씩 붙여 넣는다.

```powershell
cd C:\AI\trading_system\trading_system
$env:TRADE_MODE='PAPER'
.\.venv\Scripts\python.exe scripts\fetch-daily.py --mode PAPER --top 30
```

기대 화면(순서):
- `종목 선정: ka10099 코스피 + ka10001 시가총액 필드 'mac' 사용 (...미확인 필드...)`
- `ka10099 코스피 N종목 중 제외 M, 시가총액 조회 대상 K (예상 소요 약 X분 이상, 추정)`
- `교차 확인(flo_stk×cur_prc): 상위 30 중 순위 일치 n`
- `선정 30종목 ...` 과 순위 목록(1. 005930 삼성전자 ...)
- 종목마다 `005930: 2016-01-04 ~ 2026-10-07 2,6xx행` 형태
- `토큰 폐기: 완료`
- `요약: 성공 30, 실패 0, 미조회 0, 기간 2016-01-01 ~ <마지막 확정일>, 저장 <폴더>, 요약 파일 fetch-summary-<시각>.json`
- 종료 코드: 0 전부 성공, 1 일부 실패·선정 실패, 2 거부(연결 없음)

선택: 종목만 먼저 보고 싶으면 끝에 `--dry-run`을 붙인다(일봉 조회 0회). 그 뒤 일봉은 선정 결과를 그대로 넘겨 받는다(순위 재계산 없음).

```powershell
.\.venv\Scripts\python.exe scripts\fetch-daily.py --mode PAPER --top 30 --dry-run
.\.venv\Scripts\python.exe scripts\fetch-daily.py --mode PAPER --codes var\backtest\data\kiwoom-ka10081-adj-20261007\meta\universe.csv
```
(날짜 `20261007`은 첫 명령이 출력한 저장 폴더 이름으로 바꾼다.)

## 4. 예상 소요 시간 [추정]
모의 호출 제한은 TR별 1초 1회(kiwoom-kr.md §2), 도구 간격 1.1초.

| 단계 | 호출 수 [추정] | 시간 [추정] |
|---|---|---|
| ka10099 (코스피·ETF·ETN) | 3회 + 연속조회 쪽 수(미확인) | 수 초 |
| ka10001 시가총액 | 코스피 후보 약 800종목 → 약 800회 | 약 15분 |
| ka10081 일봉 | 30종목 × 약 5쪽(한 쪽 약 600행 가정, 2016~ 약 2,650행) = 약 150회 | 약 3분 |
| 합계 | 약 950회 (일일 상한 40,000회 안쪽) | **약 20분, 응답 지연 포함 최대 30분** |

`--codes`로 받으면 ka10001을 건너뛰어 약 3분.

## 5. 결과 확인
```powershell
cd C:\AI\trading_system\trading_system
Get-ChildItem var\backtest\data\kiwoom-ka10081-adj-20261007 -Filter *.csv | Measure-Object
Get-Content var\backtest\data\kiwoom-ka10081-adj-20261007\meta\universe.csv -Encoding UTF8
Get-Content var\backtest\data\kiwoom-ka10081-adj-20261007\005930.csv -TotalCount 3
Get-Content var\backtest\data\kiwoom-ka10081-adj-20261007\005930.csv -Tail 2
```
기대: CSV 30개(`Count : 30`), universe.csv에 순위·코드·종목명·시가총액, 일봉 첫 줄 `date,open,high,low,close,volume`, 마지막 줄 날짜 = 마지막 확정 거래일.
확인할 것: 순위가 상식과 맞는지(삼성전자·SK하이닉스 상위), 우선주·ETF가 없는지, 교차 확인 일치 수, 첫 봉이 2016보다 늦은 종목 표시(상장일 또는 제공 범위 한계 — 미확인).

부수 파일은 `meta\`에 둔다(universe.csv, excluded.csv, fetch-summary-<시각>.json). 백테스트 `--data`는 폴더 바로 아래 `*.csv`를 모두 일봉으로 읽으므로 같은 위치에 두지 않는다.

## 6. 실패 시
| 화면 | 뜻 | 할 일 |
|---|---|---|
| `일봉 수집 거부(연결 없음): 본 프로세스 하트비트가 n초 전` | run이 아직 살아 있거나 막 끔 | run 창이 닫혔는지 확인, 30초 뒤 같은 명령 |
| `... 실행 잠금 보유` | run 프로세스가 남아 있음 | run 창 확인 후 다시 |
| `... TRADE_MODE` / `connection_approved` / `.env.paper` | 연결 조건 미충족 | `$env:TRADE_MODE='PAPER'` 줄을 다시 실행, 설정은 시작 점검과 같음 |
| `... 이미 일봉 CSV가 있음` | 덮어쓰기 방지 | 다른 `--out` 지정 또는 실패 종목만 `--codes`로 |
| `종목 선정 실패 ... 'mac' 없음` | 시가총액 필드가 문서·추정과 다름 | 더 진행하지 말고 화면을 에이전트에 전달(필드 확인 후 수정). 급하면 종목을 직접 적은 파일로 `--codes` |
| `종목 선정 실패 ... ka10001 조회 실패 n건` | 순위 불완전 → 일봉 조회 안 함 | 몇 분 뒤 다시. 반복되면 화면 전달 |
| `실패 <코드>: ...` (종료 코드 1) | 그 종목만 건너뜀 | 아래처럼 실패 종목만 다시 받기 |
| `중단: 연속 3종목 실패` | 호출 제한·서버 문제 가능 | 10분 뒤 미조회 종목을 `--codes`로 |
| `수집 중 주문 API 호출 시도` | 있어서는 안 되는 일 | 결과를 쓰지 말고 화면을 에이전트에 전달 |
| `토큰 폐기: 실패` | 토큰은 만료 시각에 정리됨 | 다음 날 run 시작 전 특별 조치 없음 |

실패 종목만 다시 받기(예: 207940, 저장 폴더는 첫 실행이 출력한 것):
```powershell
cd C:\AI\trading_system\trading_system
$env:TRADE_MODE='PAPER'
Set-Content -Encoding utf8 var\backtest\retry-codes.txt "207940"
.\.venv\Scripts\python.exe scripts\fetch-daily.py --mode PAPER --codes var\backtest\retry-codes.txt --out var\backtest\data\kiwoom-ka10081-adj-20261007
```
실패·미조회 종목 목록은 `meta\fetch-summary-<시각>.json`의 `failed`·`not_queried`에 있다.

## 7. 가정·미확인
- ka10001 `mac`·`flo_stk` 존재·단위: 미확인(첫 실행으로 확인 → kiwoom-kr.md에 기록 필요). 차단: 없으면 도구가 중단하므로 잘못된 순위로 진행하지 않음.
- 우선주 코드 끝자리 규칙: 가정(과잉 제외 쪽).
- ka10081 한 쪽 행 수, 2016년 이전 수정주가 제공 범위: 미확인(소요 시간 추정에만 영향).
- 키움 모의 ka10081 일봉이 KRX 정규장 기준인지(K-U19): 미확인 — 백테스트 해석 시 유의.
- 생존편향: 오늘 시가총액 상위로 과거를 고르므로 생존편향이 있다. 백테스트 보고서의 한계로 적는다(04 §2).
