# 바이낸스 Spot API — 크립토 현물 (M2) 확인 기록

- 문서명: binance-spot.md (브로커 API 확인 기록)
- 버전: v0.3
- 상태: draft
- 작성: 2026-10-04 (에이전트)
- 근거: 사용자 지시(2026-10-04 "바이낸스로 진행", 공개 문서 조사)
- 범위: **공개 문서 열람만. 로그인·계정 생성·API 키 사용·API 호출(공개 시세·테스트넷 포함) 없음.**
- 확인일: v0.2까지 2026-10-04. v0.3 추가·갱신 행은 2026-10-05(행에 표시). v0.3 원문 기준: 공식 GitHub master 커밋 `828ca74b809cfedbd5602df328b5f706368d483b`(2026-09-18, raw markdown 내려받아 문자열 대조). v0.2 열람 시점 커밋과 같은지는 미확인(CHANGELOG 최종일이 같아 같을 가능성 높음 — 추정).
- 증거 기준: 바이낸스 공식 문서(developers.binance.com, github.com/binance, binance.com 지원·공지)만 근거로 쓴다. 제3자 블로그·라이브러리 이슈·Binance Square 게시물은 근거로 쓰지 않는다.
- 인용 주의: 원문은 웹 조회 도구로 읽었다. 도구가 요약해서 돌려준 경우가 있어, 따옴표 문구라도 **주문 안전에 직접 영향을 주는 문구(§5 표시 ★)는 구현 착수 전에 사람이 원문과 대조**한다. 요약끼리 어긋난 항목은 미확인으로 둔다.
- 상태 표기: **확인** / **미확인**. 차단 표기는 M2 구현 착수 기준.
- ★ 인용 원문 대조: 2026-10-04 (결과 §5 표시). raw GitHub markdown(rest-api.md, web-socket-api.md, errors.md, CHANGELOG.md, user-data-stream.md, master)을 내려받아 문자열 대조. ★ 6행 모두 ✔(일치·의미 일치). 단 "같은 ID 재사용"은 REST·WS API 문서 간 범위가 달라 B-U2 미확인 유지.
- 변경 이력: v0.1 (2026-10-04) 최초 작성 / v0.2 (2026-10-04) §5 ★ 인용 원문 대조 — 원문 문장으로 교체, WS API의 "filled or expired"·취소 ID 재사용 문구 추가, -1000 결과 불명 분류를 설계 판단으로 명시, cancelReplace 409 양방향 주석, listenKey 폐지일 CHANGELOG 대조. / v0.3 (2026-10-05) CP-024 설계용 원문 대조(에이전트, 공개 문서만): B-U16~B-U21 추가(B-U17·B-U19 해소), S20·S21 추가, §3 시각 검증 의사코드, §5 HTTP 반환 코드·오류 코드별 실행 여부, §6 MARKET 명목가 기준가(참조가 우선)·avgPrice·referencePrice.

## 1. 출처
| ID | 출처 | URL | 버전·일자 |
|---|---|---|---|
| S1 | Spot API — General API Information (REST) | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-api-information | 페이지 표기 최종 수정 2026-10-02 |
| S2 | Spot API — LIMITS (REST) | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/limits | 2026-10-04 열람 |
| S3 | Spot API — General endpoints (exchangeInfo, time) | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/general-endpoints | 2026-10-04 열람 |
| S4 | Spot API — Trading endpoints | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/trading-endpoints | 2026-10-04 열람 |
| S5 | Spot API — Account endpoints | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/account-endpoints | 페이지 날짜 표기 확인 못 함 |
| S6 | Spot API — Market data endpoints | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/market-data-endpoints | 2026-10-04 열람 |
| S7 | 공식 GitHub 문서 저장소 binance/binance-spot-api-docs — `rest-api.md`, `enums.md`, `filters.md`, `errors.md`, `user-data-stream.md` | https://github.com/binance/binance-spot-api-docs (raw: https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/) | master 2026-10-04 열람. 커밋 해시 미기록 |
| S8 | Spot API Changelog | https://developers.binance.com/docs/binance-spot-api-docs , raw `CHANGELOG.md` | "Last Updated: 2026-09-18" |
| S9 | Spot Testnet — General Info | https://developers.binance.com/docs/binance-spot-api-docs/testnet/general-info , raw `testnet/general-info.md` | 페이지 표기 최종 수정 2026-10-02 |
| S10 | Spot Test Network 사이트 | https://testnet.binance.vision/ | 2026-10-04 열람 |
| S11 | Demo Mode for SPOT Trading — General Info | https://developers.binance.com/docs/binance-spot-api-docs/demo-mode/general-info | 페이지 표기 최종 수정 2026-10-02 |
| S12 | WebSocket API — General API information | https://developers.binance.com/docs/binance-spot-api-docs/websocket-api/general-api-information | 페이지 표기 최종 수정 2026-10-02 |
| S13 | WebSocket API — User Data Stream requests | https://developers.binance.com/docs/binance-spot-api-docs/websocket-api/user-data-stream-requests | 날짜 표기 확인 못 함 |
| S14 | User Data Streams | https://developers.binance.com/docs/binance-spot-api-docs/user-data-stream | 페이지 표기 최종 수정 2026-10-02 |
| S15 | User data stream endpoints (Deprecated) | https://developers.binance.com/docs/binance-spot-api-docs/rest-api/user-data-stream-endpoints-deprecated | 2026-10-04 열람 |
| S16 | 바이낸스 FAQ — How to Create API Keys on Binance? | https://www.binance.com/en/support/faq/how-to-create-api-keys-on-binance-360002502072 | 도구 보고 기준 2025-03-20 수정 |
| S17 | 바이낸스 FAQ — How to Calculate Binance Spot Trading Fees? | https://www.binance.com/en/support/faq/how-to-calculate-binance-spot-trading-fees-e85d6e703b874674840122196b89780a | "Updated on 2026-01-06" |
| S18 | 바이낸스 공지 — Changes to Product Offerings in Korea | https://www.binance.com/en/support/announcement/changes-to-product-offerings-in-korea-fc0d84c3b19d4069aceb6a7e47262242 | 2021-08-13 |
| S20 | FIX API — Client order ID 정규식 | https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/fix-api.md | 커밋 828ca74b (2026-09-18), 2026-10-05 열람 |
| S21 | SPOT Glossary — orderId 정의 | https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/faqs/spot_glossary.md | 커밋 828ca74b, 2026-10-05 열람 |
| S19 | 바이낸스 Terms / List of Prohibited Countries | https://www.binance.com/en/terms , https://www.binance.com/en/legal/list-of-prohibited-countries | **본문 열람 실패**(동적 페이지). 근거로 쓰지 않음 |

## 2. 환경
### 2.1 Spot Testnet
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| REST | `https://testnet.binance.vision/api` (대체 `https://api1.testnet.binance.vision/api`) | S9 | 확인 |
| WebSocket API | `wss://ws-api.testnet.binance.vision/ws-api/v3` (`:9443` 변형 있음) | S9 | 확인 |
| 시장 스트림 | `wss://stream.testnet.binance.vision/stream` (`:9443` 변형), SBE·FIX 별도 | S9 | 확인 |
| 실전 대응 URL (참고, LIVE 범위 밖) | REST `https://api.binance.com` 외 api-gcp, api1~api4. WS API `wss://ws-api.binance.com/ws-api/v3` | S1, S9 | 확인 |
| 키 발급 | testnet.binance.vision에 로그인 후 API 키 생성. 사이트 로그인 수단은 "Log In with GitHub" | S9, S10 | 확인 |
| 키 유형 | HMAC-SHA-256 외 RSA(2048~4096bit, PKCS#1 v1.5)·Ed25519 지원 | S9, S10 | 확인 |
| 지원 엔드포인트 | `/api`만. "/sapi endpoints are not available" | S9 | 확인 |
| 자금 | 가입 시 여러 자산의 가상 잔고 자동 지급. "All funds on the Spot Test Network are virtual, and can not be transferred in/out" | S9 | 확인 |
| **리셋** | 약 **월 1회** 빈 상태로 리셋, **사전 공지 없음**. 미체결·체결 주문 전부 삭제, 잔고 새로 지급. **API 키는 유지**(2020-08부터) | S9, S10 | 확인 |
| 제한·필터 | "IP Limits, Order Rate Limits, Exchange Filters and Symbol Filters on the Spot Test Network are generally the same as on the Spot API." 현재 값은 테스트넷 exchangeInfo로 확인하라고 안내 | S9 | 확인 ("generally" — 동일 보장 아님) |
| 시세·호가 | 실전과 **독립된 호가창**(Demo Mode 비교표 기준) | S11 | 확인 |
| 기능 | 실전보다 신기능이 먼저 들어오기도 함(Demo Mode 비교표) | S11 | 확인 |
| klines vs uiKlines | 테스트넷에서는 두 엔드포인트가 항상 같은 데이터 | S10 | 확인 |
| 거래 가능 심볼 목록 | 문서에 목록 없음 (exchangeInfo 조회 필요 — 이번 조사에서 호출 금지) | — | **미확인** B-U4 |
| 서명 percent-encode 규칙 | 테스트넷에 이미 적용 | S8 (2025-12-17) | 확인 |

### 2.2 Demo Mode (Demo Trading) — 테스트넷과 별개
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| 제공 시작 | 2026-01-29 changelog에 Spot Demo Mode 제공 | S8, S11 | 확인 |
| 접근 | **바이낸스 실계정 로그인** 후 Binance Demo Trading의 API Key Management(`https://demo.binance.com/en/my/settings/api-management`)에서 키 생성 | S11 | 확인 |
| URL | REST `https://demo-api.binance.com/api`, WS API `wss://demo-ws-api.binance.com/ws-api/v3`, 스트림 `wss://demo-stream.binance.com/ws`·`/stream`, FIX `demo-fix-*.binance.com` | S11 | 확인 |
| 테스트넷과 차이 | 잔고: 테스트넷 월 1회 자동 리셋 / Demo는 UI에서 원할 때 리셋. 기능: Demo는 "always has the same features as the live exchange". 시세: Demo는 실전과 "similar", 테스트넷은 독립. 거래 규칙(IP 제한·미체결 주문 수·필터): Demo는 "exactly the same as the live exchange", 테스트넷은 "generally" 같음 | S11 | 확인 |
| 실계정 KYC 등 전제 | Demo 키 생성에 실계정이 필요하다는 것까지만 확인. 지역·KYC 요건 문구 없음 | S11 | **미확인** B-U6 |

### 2.3 지역 이용 관련 공식 문구
| 항목 | 내용 | 출처 | 상태 |
|---|---|---|---|
| 테스트넷 지역 제한 | S9·S10에 지역 제한 문구 **없음** | S9, S10 | 확인(문구 부재). 이용 약관 적용 여부는 미확인 |
| Demo Mode 지역 제한 | S11에 지역 제한 문구 없음 | S11 | 확인(문구 부재) |
| 한국 관련 공식 공지 (2021-08-13) | "As Binance constantly evaluates its product and service offerings to proactively comply with local regulations, we will discontinue the following offerings in Korea, effective immediately: KRW trading pairs / KRW payment options / P2P merchant applications / Korean language website support" | S18 | 확인(원문 인용, 2021년 공지) |
| 금지 국가 목록·이용약관 | 공식 페이지 본문을 읽지 못함(동적 페이지). 검색 결과 요약에 북한(DPRK) 포함 문구가 보였으나 원문 미열람이므로 근거로 쓰지 않음. **대한민국 거주자의 binance.com 계정·API 이용 가능 여부는 확인하지 못함** | S19 | **미확인** B-U6 |

## 3. 인증·키
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| 서명 방식 | HMAC(서명 대소문자 무관), RSA(PKCS#8, 대소문자 구분), Ed25519(대소문자 구분, 성능·보안상 권장) | S1 | 확인 |
| 실전 키 종류 | System-generated = HMAC(API key + Secret). Self-generated = Ed25519 또는 RSA(사용자가 키쌍 생성) | S16 | 확인 |
| 생성 전제 (실전) | 2FA, Spot 지갑 입금, 신원 인증 완료 | S16 | 확인 |
| 권한·IP 제한 (실전) | "Unrestricted-IP-Access HMAC API Key(s) won't have any permission other than reading." / "Adding IP access (IPv4 format) restrictions is mandatory to enable withdrawal permission." → 거래 권한은 IP 제한 설정 또는 self-generated 키 필요 | S16 | 확인 |
| 출금 없이 거래만 허용 | 권한 항목이 분리되어 있음(Spot & Margin Trading / Withdrawals). 세부 항목 이름은 도구 요약 기준 | S16 | 확인(항목 분리). 화면 문구는 사용자 확인 |
| 테스트넷 권한·IP 제한 설정 | 문서에 언급 없음 | — | **미확인** B-U5 |
| 시각 검증 | `timestamp < (serverTime + 1 second) && (serverTime - timestamp) <= recvWindow` 이면 처리. recvWindow 기본 5000ms, 최대 60000ms, 마이크로초 지원 | S1, S7 | 확인 |
| 서버 시각 | `GET /api/v3/time` (weight 1) | S3 | 확인 |
| 시각 오류 | -1021 INVALID_TIMESTAMP ("Timestamp for this request is outside of the recvWindow." / "Timestamp for this request was 1000ms ahead of the server's time.") | S7 errors.md | 확인 |
| 시각 검사 위치 (2026-10-05) | rest-api.md "Timing security" 의사코드: `if (timestamp < (serverTime + 1 second) && (serverTime - timestamp) <= recvWindow) { // forward request to Matching Engine } else { // reject request }` (원문 인용, WS API 문서도 같은 의사코드) → **-1021은 매칭엔진 전달 전 거부** | S1, S7 | 확인 |
| 서명 오류 | -1022 INVALID_SIGNATURE. 2026-01-15 07:00 UTC부터 서명 전 payload percent-encode 필수, 위반 시 -1022 | S7, S8 | 확인 |
| 키·IP·권한 거부 | -2015 "Invalid API-key, IP, or permissions for action." | S7 | 확인 |

## 4. 호출 제한
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| 기준 | REQUEST_WEIGHT·RAW_REQUESTS는 **IP 기준**, ORDERS(미체결 주문 수)는 **계정 기준** | S2, S12 | 확인 |
| 실제 한도 값 | exchangeInfo `rateLimits`로 제공. 문서 예시는 페이지마다 다름(REQUEST_WEIGHT 6000/1분, ORDERS 50/10초 또는 10/1초, RAW_REQUESTS 61000/5분) | S3, S7 enums | 예시만 확인. 실제 값 **미확인** B-U1 |
| RAW_REQUESTS 변경 | 2026-04-02 07:00 UTC부터 300,000/5분 | S8 (2026-03-27) | 확인 |
| 주문 weight 변경 | 같은 시점부터 주문 관련 엔드포인트(`POST/DELETE /api/v3/order`, `cancelReplace` 등)는 **성공 시 weight 0**, 실패 시 문서 weight 부과 | S8 (2026-03-27) | 확인 |
| 사용량 헤더 | `X-MBX-USED-WEIGHT-(intervalNum)(intervalLetter)`, `X-MBX-ORDER-COUNT-(intervalNum)(intervalLetter)`. 거부·실패 주문 응답에는 ORDER-COUNT 헤더가 없을 수 있음 | S1, S2 | 확인 |
| 429 | 한도 초과. `Retry-After` 헤더(초) | S1, S2 | 확인 |
| 418 | 429 이후에도 계속 요청하면 IP 자동 차단. 반복 시 **2분~3일**로 늘어남 | S1, S2 | 확인 |
| 오류 코드 | -1003 TOO_MANY_REQUESTS("Way too much request weight used; IP banned until %s." 포함), -1015 TOO_MANY_ORDERS("Too many new orders; current limit is %s orders per %s.") | S7 errors.md | 확인 |
| 미체결 주문 수 | 계정 기준. 주문이 계속 체결되면 계속 주문 가능 | S1, S2 | 확인. 세부 규칙 문서(Unfilled Order Count FAQ)는 미열람 |
| 처리 타임아웃 | 10초 초과 시 -1007 TIMEOUT. 매칭엔진 실패를 보장하지 않음 | S2, S7 | 확인 |
| WebSocket API 연결 | 연결 1개 24시간 유효. 서버 ping 20초마다, 60초 안에 pong 없으면 끊음. IP당 5분에 연결 시도 300회 | S12 | 확인 |
| WebSocket API 한도 | REQUEST_WEIGHT는 IP 기준 모든 연결 합산, ORDERS는 계정 기준. 응답의 `rateLimits` 필드로 사용량 보고(`returnRateLimits`로 끌 수 있음). 429·418 동일 | S12 | 확인 |
| 서버 종료 알림 | `serverShutdown` 이벤트(종료 10분 전) 수신 시 새 연결로 갈아탈 것 | S8 (2026-05-06, 06-09) | 확인 |

## 5. 주문
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| 주문 유형 | LIMIT, MARKET, STOP_LOSS, STOP_LOSS_LIMIT, TAKE_PROFIT, TAKE_PROFIT_LIMIT, LIMIT_MAKER | S7 enums | 확인 |
| 필수 파라미터 | LIMIT: timeInForce·quantity·price / MARKET: quantity 또는 quoteOrderQty / LIMIT_MAKER: quantity·price | S7 rest-api | 확인 |
| timeInForce | GTC, IOC, FOK | S7 enums | 확인 |
| ✔ newClientOrderId 설명 | REST `POST /api/v3/order` 파라미터 설명: "A unique id among open orders. Automatically generated if not sent." (원문 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md — "New order (TRADE)" 표, SOR 주문 표도 같은 문구). WS API `order.place`는 "Arbitrary unique ID among open orders. Automatically generated if not sent" | S7 rest-api, web-socket-api | 확인 (일치) |
| ✔ 같은 ID 재사용 | REST 같은 칸 원문: "Orders with the same `newClientOrderID` can be accepted only when the previous one is filled, otherwise the order will be rejected." (원문 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md). **문서 간 차이**: WS API `order.place`·`order.cancelReplace` Notes는 "A new order with the same `clientOrderId` is accepted only when the previous one is filled or expired." (만료 포함), cancelReplace Notes는 "The new order can reuse old `clientOrderId` of the canceled order.", 취소 Notes는 "`newClientOrderId` will replace `clientOrderId` of the canceled order, freeing it up for new orders." (원문 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/web-socket-api.md) → **체결·만료·취소된 주문의 ID는 재사용이 받아들여질 수 있다**(REST 문구는 "filled"만 언급, WS 문구가 더 넓음). 판정 보존 기간 문구 없음. 취소된 주문의 `clientOrderId`가 취소 요청의 새 ID로 바뀐다는 점 — 취소 후 원래 ID로 조회되는지는 **미확인** | S7 rest-api, web-socket-api | 재사용 허용 범위 문서 간 불일치 → **미확인** B-U2 (설계 결론은 아래 "멱등성 결론" 유지·강화) |
| ✔ 중복 거부 메시지 | errors.md "-2010 NEW_ORDER_REJECTED" 계열 메시지 표 원문: "Duplicate order sent." \| "The `clOrdId` is already in use." (원문 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/errors.md). 표 제목은 "Messages for -1010 ERROR_MSG_RECEIVED, -2010 NEW_ORDER_REJECTED, -2011 CANCEL_REJECTED, and -2038 ORDER_AMEND_REJECTED" — 메시지별 코드 매핑은 표에 없음 | S7 errors.md | 확인 (의미 일치) |
| 멱등성 결론 | 서버 중복 거부는 **열린 주문 범위**의 보호일 뿐, 체결 후(WS 문서 기준 만료·취소 후 포함) 같은 ID 재전송은 새 주문이 될 수 있음 → **브로커 멱등성으로 간주하지 않는다** (CLAUDE.md §8-5) | S4, S7 | 확인된 사실에서 도출 |
| newOrderRespType | ACK / RESULT / FULL. MARKET·LIMIT 기본 FULL, 그 외 기본 ACK. FULL은 `fills`(price, qty, commission, commissionAsset, tradeId) 포함 | S4, S7 | 확인 |
| 주문 상태 | NEW("accepted by the engine"), PENDING_NEW(주문 목록의 working order 체결 대기), PARTIALLY_FILLED, FILLED, CANCELED("canceled by the user"), PENDING_CANCEL("Currently unused"), REJECTED("not accepted by the engine and not processed"), EXPIRED(주문 규칙 또는 거래소에 의해 취소 — 예: FOK 미체결, IOC·MARKET 부분 체결, 유지보수 중 취소), EXPIRED_IN_MATCH(STP로 만료) | S7 enums | 확인 |
| 만료 사유 | 만료 주문 조회 응답에 `expiryReason` 필드가 붙을 수 있음 | S8 (2026-05-06) | 확인 |
| 실행 유형(executionReport x) | NEW, CANCELED, REPLACED, REJECTED, TRADE, EXPIRED, TRADE_PREVENTION | S7 enums | 확인 |
| STP 모드 | NONE, EXPIRE_MAKER, EXPIRE_TAKER, EXPIRE_BOTH, DECREMENT, TRANSFER. 2026-04-02부터 TRANSFER 전 심볼 적용. 심볼별 기본값 `defaultSelfTradePreventionMode` | S7, S8 | 확인 |
| ✔ 5XX | "HTTP `5XX` return codes are used for internal errors; the issue is on Binance's side. It is important to **NOT** treat this as a failure operation; the execution status is **UNKNOWN** and could have been a success." (원문 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md "HTTP Return Codes") | S1 | 확인 (일치) |
| ✔ WebSocket API 5xx | "**Important:** If a response contains 5xx status code, it **does not** necessarily mean that your request has failed. Execution status is _unknown_ and the request might have actually succeeded. Please use query methods to confirm the status. You might also want to establish a new WebSocket connection for that." (원문 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/web-socket-api.md) | S12 | 확인 (의미 일치 — v0.1 인용은 강조 위치만 다름. 원문은 조회로 확인하라고 명시) |
| ✔ 타임아웃 | -1007 TIMEOUT "Timeout waiting for response from backend server. Send status unknown; execution status unknown." / -1006 UNEXPECTED_RESP "An unexpected response was received from the message bus. Execution status unknown." / -1000 UNKNOWN "An unknown error occurred while processing the request." (원문 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/errors.md). rest-api.md "APIs have a timeout of 10 seconds ..." 문장에도 -1007 문구 동일 | S1, S7 | -1007·-1006 확인 (일치) → **결과 불명**. -1000 원문은 실행 상태를 언급하지 않음 → 결과 불명 처리는 **설계 판단**(보수적 분류) |
| HTTP 반환 코드 (2026-10-05) | 원문: "HTTP `4XX` return codes are used for malformed requests; the issue is on the sender's side." / "HTTP `403` return code is used when a WAF (Web Application Firewall) rule has been violated. ..." / "HTTP `409` return code is used when a cancelReplace order partially succeeds. ..." / "HTTP `429` return code is used when breaking a request rate limit." / "HTTP `418` return code is used when an IP has been auto-banned for continuing to send requests after receiving `429` codes." LIMITS: "Requests fail with HTTP status code 429 when you exceed the request rate limit." 429·418에 "처리되지 않음" 명시 문구 없음. **4XX를 일괄 미실행으로 볼 수 없음**(409 부분 성공, 403 WAF 포함) | S1, S2 | 확인 |
| 오류 코드별 실행 여부 (2026-10-05) | 미실행이 원문으로 명시된 것: -1013("didn't reach the Matching Engine"), -1021(위 의사코드). -2010 NEW_ORDER_REJECTED·-2011 CANCEL_REJECTED: 메시지 표 머리말 "This code is sent when an error has been returned by the matching engine." 실행 여부 언급 없음: -1000, -1003, -1015, -1022, -1100~-1106, -1111, -2015 | S7 errors.md | 확인 → 언급 없는 코드는 B-U20 |
| -2010 메시지 표 (2026-10-05) | 표 제목 "Messages for -1010 ERROR_MSG_RECEIVED, -2010 NEW_ORDER_REJECTED, -2011 CANCEL_REJECTED, and -2038 ORDER_AMEND_REJECTED". 메시지와 코드 매핑 없음. 같은 ID 주문 존재를 시사하는 것은 "Duplicate order sent." / "The `clOrdId` is already in use."뿐. 취소 거부의 "Unknown order sent."는 이미 체결·만료됐을 가능성 배제 못 함(설계 판단) | S7 errors.md | 확인 |
| 매칭엔진 미도달 | -1013 INVALID_MESSAGE "The request is rejected by the API. (i.e. The request didn't reach the Matching Engine.)" → 명시 거부 | S7 | 확인 |
| 주문 조회 | `GET /api/v3/order` (weight 4, Data Source "Memory => Database" — 2026-10-05). "Either `orderId` or `origClientOrderId` must be sent." 둘 다 보내면 orderId 우선 검색 후 origClientOrderId 검증 | S5, S7 | 확인 |
| 조회 없음 | -2013 NO_SUCH_ORDER "Order does not exist." / "Unknown order sent." | S7 | 확인. 접수 직후 조회 시 "없음"이 확정인지(데이터 소스 지연)는 **미확인** B-U3 |
| 보관 | -2026 ORDER_ARCHIVED: 체결 수량 없이 취소·만료된 지 90일 넘은 주문은 보관 처리 | S7 | 확인 |
| 데이터 소스 | 엔드포인트마다 Matching Engine / Memory / Database 중 하나(지연 순) | S1 | 확인. 엔드포인트별 값은 구현 시 표기 확인 |
| 취소 | `DELETE /api/v3/order`, orderId 또는 origClientOrderId. `cancelRestrictions`: ONLY_NEW / ONLY_PARTIALLY_FILLED | S4 | 확인 |
| 취소 후 신규 | `POST /api/v3/order/cancelReplace`. cancelReplaceMode STOP_ON_FAILURE(취소 실패 시 신규 시도 안 함) / ALLOW_FAILURE(취소 실패해도 신규 시도). HTTP 200 둘 다 성공, 400 취소 실패·신규 미시도, 409 한쪽만 성공, 429 한도. 미시도 신규도 미체결 주문 수 +1. (원문 대조 2026-10-04: 일반 정보 문장은 "HTTP `409` return code is used when a cancelReplace order partially succeeds. (i.e. if the cancellation of the order fails but the new order placement succeeds.)"로 한 방향만 예시하나, cancelReplace 응답 표는 취소 성공·신규 실패도 409 — https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/rest-api.md. 409 수신 시 cancelResult·newOrderResult로 어느 쪽인지 판별 필요) | S4, S7 | 확인. 원자성 보장 문구는 확인 못 함 → 원자적이라고 가정하지 않음 |
| 수량 감소 정정 | `PUT /api/v3/order/amend/keepPriority`(weight 4), newQty < 원수량, 대기 순서 유지 | S4 | 확인 |
| 주문 테스트 | `POST /api/v3/order/test` 존재(매칭엔진 미전송) | S4 | 확인(이름만). 상세 미열람 |
| 주문 거부 사유 예 | "Account has insufficient balance for requested action.", "Order would immediately match and take."(LIMIT_MAKER), "Market is closed.", "This action is disabled on this account." 등 | S7 | 확인 |

## 6. 필터·수량 규칙 (exchangeInfo)
| 필터 | 규칙 | 출처 | 상태 |
|---|---|---|---|
| exchangeInfo | `GET /api/v3/exchangeInfo` weight 20. 파라미터 symbol·symbols·permissions·showPermissionSets·symbolStatus. 심볼 키: status, baseAssetPrecision, quoteAssetPrecision, baseCommissionPrecision, quoteCommissionPrecision, orderTypes, quoteOrderQtyMarketAllowed, cancelReplaceAllowed, amendAllowed, isSpotTradingAllowed, filters, permissions, permissionSets, defaultSelfTradePreventionMode 등 | S3, S7 | 확인 |
| PRICE_FILTER | price ≥ minPrice, price ≤ maxPrice, `price % tickSize == 0`. 값 0이면 해당 규칙 비활성 | S7 filters | 확인 |
| MARKET 주문 명목가 기준가 (2026-10-05) | filters.md 원문: "Since `MARKET` orders have no `price`, an `average of previous trade prices` is used instead." / "When a non-null reference price for the symbol exists, it is used as `price`." / "When a non-null reference price for the symbol does not exist, then the volume weighted average price over the preceding `avgPriceMins` minutes is used as `price`." / "If `avgPriceMins` is 0, then the last price is used as `price`." `GET /api/v3/referencePrice` w2·Memory(미설정 이력이면 -2043), `GET /api/v3/avgPrice` w2·Memory(응답 mins·price·closeTime) | S7 filters.md, rest-api.md | 확인. avgPrice 응답 값이 필터 평균가와 같다는 문구는 **미확인** |
| PERCENT_PRICE / PERCENT_PRICE_BY_SIDE | 평균가(참조가 있으면 참조가, 없으면 avgPriceMins 거래량가중평균) 대비 배수 범위. BY_SIDE는 매수 bidMultiplierUp/Down, 매도 askMultiplierUp/Down | S7 filters | 확인 |
| LOT_SIZE | quantity ≥ minQty, ≤ maxQty, `quantity % stepSize == 0` | S7 filters | 확인(도구 보고 문구. 원문 대조 권장) |
| MARKET_LOT_SIZE | MARKET 주문 수량에 같은 형태 규칙 | S7 filters | 확인 |
| MIN_NOTIONAL / NOTIONAL | price×quantity ≥ minNotional (NOTIONAL은 ≤ maxNotional도). applyToMarket / applyMinToMarket·applyMaxToMarket. MARKET의 price는 참조가가 있으면 참조가, 없으면 avgPriceMins 거래량가중평균가(위 "MARKET 주문 명목가 기준가" 행, 2026-10-05 정정) | S7 filters | 확인 |
| MAX_NUM_ORDERS | 심볼당 열린 주문 수 상한(일반+알고) | S7 filters | 확인 |
| MAX_NUM_ALGO_ORDERS / MAX_POSITION / ICEBERG_PARTS / TRAILING_DELTA | 존재·규칙 확인. MAX_POSITION = free + locked + 열린 매수 주문 수량 합 | S7 filters | 확인 |
| EXCHANGE_MAX_NUM_ORDERS | 계정 전체 열린 주문 수 상한 | S7 filters | 확인 |
| 필터 위반 | -1013 계열 "Filter failure: PRICE_FILTER / LOT_SIZE / NOTIONAL / MIN_NOTIONAL / MAX_NUM_ORDERS ..." | S7 errors | 확인 |
| 정밀도 | -1111 BAD_PRECISION "Parameter '%s' has too much precision." 소수 자릿수 안내는 exchangeInfo의 precision 필드·필터로 판단 | S7 | 확인. 문자열 소수 표기(지수 표기 허용 여부 등) 지침 **미확인** B-U11 |
| Price Range Execution Rule | 2026-03-09 도입. `GET /api/v3/executionRules`, `/api/v3/referencePrice` 등 신규 | S8 | 확인. 주문 거부 영향 범위 **미확인** B-U12 |

## 7. 체결·계좌 조회
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| 계좌 | `GET /api/v3/account` weight 20, `omitZeroBalances`. 응답 balances(free, locked), canTrade, permissions, commissionRates, uid | S5, S7 | 확인. `uid` 등 계정 식별값은 로그 마스킹 대상 |
| 체결 내역 | `GET /api/v3/myTrades` weight 20(orderId 지정 시 5), symbol 필수, Data Source Memory => Database, 응답에 **`quoteQty`**(체결 금액, 2026-10-05 확인). startTime~endTime 24시간 이내, fromId 이상 조회, limit 최대 1000. 응답 id, orderId, commission, commissionAsset, isBuyer, isMaker 등 | S5, S7 | 확인 |
| 열린 주문 | `GET /api/v3/openOrders` weight 6(심볼 지정) / 80(심볼 생략), symbol 선택("If the symbol is not sent, orders for all symbols will be returned in an array."), Data Source Memory => Database | S5, S7 | 확인 (2026-10-05 보강) |
| 전체 주문 | `GET /api/v3/allOrders` weight 20, symbol 필수, **Data Source Database**, "The time between `startTime` and `endTime` can't be longer than 24 hours.", 응답 `cummulativeQuoteQty`("For some historical orders `cummulativeQuoteQty` will be < 0, meaning the data is not available at this time."). WS API allOrders는 시간 지정 시 orderId 무시, "Orders are filtered by `time` of the last execution status update." | S5, S7 | 확인 (2026-10-05 보강) |
| 수수료율 | `GET /api/v3/account/commission` weight 20 | S5 | 확인 |
| **사용자 데이터 스트림 (현행)** | "Subscribe via the WebSocket API using an API Key." → WebSocket API `userDataStream.subscribe`(session.logon 후, Ed25519 인증 세션) 또는 `userDataStream.subscribe.signature`(로그온 없이 서명 요청). 각 weight 2. `userDataStream.unsubscribe`, `session.subscriptions` | S13, S14, S8 | 확인 (메서드명 원문 대조 2026-10-04: `userDataStream.subscribe`·`userDataStream.unsubscribe`는 CHANGELOG 2024-12-09, `userDataStream.subscribe.signature`·`session.subscriptions`는 2025-08-12). signature 방식 키 유형: CHANGELOG 2025-08-12 원문 "This also doesn’t require an Ed25519 API Key, and can work with any API Key type." (https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/CHANGELOG.md) — B-U9 해소 후보(현행 web-socket-api.md 본문 문구는 미대조) |
| listenKey (폐지) | listenKey 방식 deprecated(2025-04-07, 2025-10-24). 2026-01-21 공지: **2026-02-20 07:00 UTC부터** `POST/PUT/DELETE /api/v3/userDataStream`, WS API `userDataStream.start/ping/stop` 사용 불가 | S8, S15 | 확인 (CHANGELOG 2026-01-21 항목 원문 "will no longer be available starting from **2026-02-20,07:00 UTC**" 대조 2026-10-04, https://raw.githubusercontent.com/binance/binance-spot-api-docs/master/CHANGELOG.md) → 설계에서 listenKey 사용 금지 |
| 이벤트 | outboundAccountPosition(잔고 변경), balanceUpdate(입출금·이체), executionReport(주문), listStatus(주문 목록), eventStreamTerminated(구독 종료), externalLockUpdate | S14 | 확인 |
| executionReport 필드 | s 심볼, c clientOrderId, C 원 clientOrderId(취소 대상), S side, o 유형, f TIF, q 수량, p 가격, x 실행 유형, X 주문 상태, r 거부 사유, i orderId, **l 이번 체결 수량, z 누적 체결 수량, L 이번 체결 가격, n 수수료, N 수수료 자산, t 체결 ID**, I 실행 ID, T 거래 시각, O 생성 시각, Z 누적 체결 금액(quote), Y 이번 체결 금액, m maker 여부, v STP 시 prevented match id | S7 user-data-stream.md | 확인 (평균가 = Z / z) |
| 전달 보장·순서·재연결 시 재전송 | 문서에서 확인 못 함 | — | **미확인** B-U8 |
| 타임스탬프 단위 | JSON 기본 밀리초 (`X-MBX-TIME-UNIT: MICROSECOND`로 변경 가능) | S1, S14 | 확인 |

## 8. 시장 데이터
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| klines | `GET /api/v3/klines` weight 2. symbol, interval, startTime, endTime, timeZone(기본 0=UTC), limit 기본 500·최대 1000 | S6, S7 | 확인 |
| 간격 | 1s, 1m, 3m, 5m, 15m, 30m, 1h, 2h, 4h, 6h, 8h, 12h, 1d, 3d, 1w, 1M | S6 | 확인 |
| 시간대 | "Klines are uniquely identified by their open time". timeZone 지정 시 간격 경계는 그 시간대로 해석, **startTime·endTime은 항상 UTC**. 허용 형식 `-1:00`, `05:45`, `0`, `8` 등, 범위 [-12:00, +14:00] | S6, S7 | 확인 → 1d 봉 경계 기본 UTC 00:00 (KST 09:00) |
| 응답 | [openTime, open, high, low, close, volume, closeTime, quoteVolume, tradeCount, takerBuyBase, takerBuyQuote, ignore] | S6 | 확인 |
| 심볼 상태 | TRADING, END_OF_DAY, HALT, BREAK, CANCEL_ONLY(2026-07-01 추가). exchangeInfo `symbolStatus`로 필터 가능 | S3, S7, S8 | 값 확인. 각 값의 의미(설명 문구)는 문서에 없음 → **미확인** B-U13 |
| 상장폐지 | 공지(Announcement)로 안내. API의 사전 표시 필드·상태 전이 순서는 확인 못 함 | — | **미확인** B-U13 |
| 24/7 | 크립토 현물은 휴장 개념이 없음을 전제로 하나, 유지보수 중 주문 취소(EXPIRED 설명) 존재 | S7 enums | 유지보수 취소는 확인. 정기 점검 일정 공지 경로 **미확인** B-U14 |

## 9. 수수료
| 항목 | 값 | 출처 | 상태 |
|---|---|---|---|
| 기본 | 체결된 거래마다 수수료. 일반 사용자 예시 요율 0.1%. 등급별 요율은 Fees 페이지 참조 | S17 | 확인(예시). 실제 계정 요율은 `account/commission`으로 확인 |
| 수수료 자산 | "Trading fees are always charged in the asset you receive." (예: ETH/USDC 매수 → ETH로 부과) | S17 | 확인 → 매수 시 받은 수량에서 차감되어 보유 수량 < 체결 수량 가능 |
| BNB 할인 | "Using BNB to pay for trading fees will grant you a 25% discount." (BNB 차감 기능 켜야 함) | S17 | 확인. BNB 부족 시 처리 **미확인** B-U10 |
| 응답 필드 | FULL 응답 fills의 commission·commissionAsset, myTrades의 commission·commissionAsset, executionReport n·N | S4, S5, S7 | 확인 |
| 테스트넷 수수료 | 문서에 없음 | — | **미확인** B-U10 |

## 10. 미확인 목록
| # | 항목 | 확인 경로 | 차단 (M2 구현 착수) |
|---|---|---|---|
| B-U1 | 실제 rateLimits 값(실전·테스트넷). 문서 예시끼리 다름 | ② 시작 점검에서 exchangeInfo 조회 | 비차단 (시작 시 exchangeInfo 값을 읽어 적용, 보수값 하한) |
| B-U2 | newClientOrderId 중복 판정 범위(취소·만료 주문 ID 재사용 허용 여부, 판정 보존 기간). 원문 대조 2026-10-04: REST "filled"만, WS API "filled or expired"·취소 ID 재사용 가능 — 문서 간 불일치, 보존 기간 문구 없음 | ① 원문 대조(완료) → ② | 비차단 (ID는 의도별 고유 생성·재사용 금지, 서버 중복 거부를 멱등성으로 보지 않음) |
| B-U3 | 접수 직후 origClientOrderId 조회의 데이터 소스 지연(-2013이 "미접수" 확정인지). 2026-10-05: Data Source 표기 확인(order·myTrades "Memory => Database", allOrders "Database") — 지연 크기는 문서에 없음 | ① 완료 → ② | 비차단 (결과 불명 해소 시 allOrders·myTrades와 교차 — IOC·시장가는 openOrders에 남지 않으므로 openOrders는 쓰지 않음, 제한 횟수 후 사람 확인. CP-024 D-111) |
| B-U4 | 테스트넷 거래 가능 심볼·호가 유동성·체결 품질 | ② exchangeInfo·관찰 | 비차단 (PAPER 한계로 기록) |
| B-U5 | 테스트넷 키 권한 설정·IP 제한 지원 여부 | ④ 사용자가 사이트 화면 확인 | 비차단 |
| B-U6 | **대한민국 거주자의 binance.com 실계정·Demo Mode·테스트넷 이용 가능 여부 및 약관 적용** (금지 국가 목록 원문 미열람) | ④ 사용자 판단 (U-13) | **해소 2026-10-04 (A-055)**: 사용자 보고 "한국 거주자 이용 가능, 현재 바이낸스로 매매 중". 약관·법규 적용 판단은 사용자(U-17, LIVE 전 재확인). 공식 원문은 여전히 미열람 |
| B-U7 | 테스트넷 무통지 리셋 감지 방법(리셋 후 내부 주문·잔고와 불일치) | ② | 비차단 (불일치 → 정지·사람 확인으로 처리, 설계 반영 필요) |
| B-U8 | 사용자 데이터 스트림 전달 보장·순서·재연결 시 누락분 재전송 | ② | 비차단 (재연결 시 조회 대조) |
| B-U9 | `userDataStream.subscribe.signature`의 지원 키 유형 | ① 원문 대조 → ② PAPER 실측 | 비차단 — 단 A-063⑨로 HMAC + signature 방식이 결정되어 이 항목이 그 전제다. 불통과 시 키 유형 재결정(Ed25519 + session.logon) |
| B-U10 | 테스트넷 수수료 적용 여부·BNB 부족 시 수수료 처리 | ② / ① | 비차단 (체결 응답의 commission 값 사용) |
| B-U11 | 소수 문자열 표기 규칙(지수 표기·후행 0 허용) | ① → ② | 비차단 (Decimal → 고정소수 문자열, stepSize·tickSize 자릿수로 절사) |
| B-U12 | Price Range Execution Rule이 주문 거부에 주는 영향 | ① | 비차단 (거부 시 명시 거부 처리) |
| B-U13 | 심볼 상태 END_OF_DAY·BREAK·CANCEL_ONLY·HALT의 의미와 상장폐지 전이 순서 | ① / ③ | 비차단 (TRADING 외 전부 신규 진입·증액 차단) |
| B-U14 | 정기 점검 공지 경로·유지보수 중 주문 취소 범위 | ① 공지 | 비차단 (EXPIRED 수신·대조로 반영) |
| B-U15 | 공식 Python SDK의 Python 3.14 호환성 (이번 조사 범위 밖) | ① | 비차단 (표준 HTTP·WebSocket 직접 구현 가능) |
| B-U16 | 고객 주문 ID(newClientOrderId·origClientOrderId·listClientOrderId) 길이·문자. 2026-10-05 대조: REST·WS 문서에 규칙 없음. FIX API만 "Client order ID fields must conform to the regex `^[a-zA-Z0-9-_]{1,36}$`" (S20). -1100 "Illegal characters found in parameter '%s'; legal range is '%s'." | ① 완료(REST 문구 없음) → ② PAPER 관찰 | 비차단 (FIX 정규식을 보수 규칙으로: 1~36자, `[A-Za-z0-9_-]`) |
| B-U17 | orderId 유일 범위. 2026-10-05 대조: "`orderId` — A field in the order response that uniquely identifies the order on a symbol." (S21) | ① 완료 | **해소**: 브로커 주문 키 = (symbol, orderId) |
| B-U18 | 조회 엔드포인트 symbol·기간·Data Source. 2026-10-05 대조: §7 행 보강(order·myTrades·allOrders symbol 필수, openOrders 선택, allOrders는 Database·24시간 창) | ① 완료 → ② Memory→Database 반영 지연 관찰 | **해소**(지연 크기는 B-U3에 남음) |
| B-U19 | 체결 금액 필드. 2026-10-05 대조: myTrades `quoteQty`, 주문 조회 `cummulativeQuoteQty`(음수 = 데이터 없음), executionReport "Z ... Cumulative quote asset transacted quantity", "Y ... Last quote asset transacted quantity (i.e. lastPrice * lastQty)", FULL `fills`에는 금액 필드 없음(price·qty·commission·commissionAsset·tradeId), TRADE_PREVENTION 시 l·L·Y = 0(CHANGELOG) | ① 완료 | **해소** |
| B-U20 | -1000·-1003·-1015·-1022·-1100~-1106·-1111·-2015·HTTP 429·418이 미실행이라는 명시 문구 (§5 오류 코드별 행) | ① 완료(문구 없음) → ② | 비차단 (설계 판단은 CP-024 D-112) |
| B-U21 | 체결 0 EXPIRED(IOC 등) 주문의 origClientOrderId 조회 가능 여부. 2026-10-05 대조: 명시 문구 없음. 제한은 -2026 "Order was canceled or expired with no executed qty over 90 days ago and has been archived."와 EXPIRED_IN_MATCH 90일 보관(CHANGELOG 2025-08-12)뿐. CHANGELOG 2023-12-04 "Fixed a bug where order query requests would incorrectly return [`-2026 ORDER_ARCHIVED`] error for newly placed orders." | ② PAPER 관찰 | 비차단 (orderId 우선 조회, 90일 미만 -2026은 이상으로 처리) |

## 11. M2 착수 전 사용자 결정 질문 (초안, 최대 5개)
| # | 질문 | 차단 |
|---|---|---|
| Q1 (U-12) | 기준 통화와 환율: 리스크 한도·손익의 기준을 USDT로 둘지, KRW로 환산할지. 환산한다면 환율 출처·적용 시각·실패 시 처리 | **결정 A-055**: 바이낸스 계좌는 USDT 기준(한도·손익), 키움은 KRW 별도. 환율 없음 — 두 계좌 합산은 보고서 참고 환산만 |
| Q2 (U-13) | PAPER 환경과 지역 이용: 테스트넷(GitHub 로그인, 독립 호가, 월 1회 무통지 리셋)과 Demo Mode(실계정 필요, 실전 유사 시세·동일 규칙) 중 무엇을 쓸지, 대한민국에서의 이용·약관 적용 판단(B-U6) | 이용 가능 사용자 보고(A-055). **결정 A-062①**: M2 PAPER-0은 테스트넷, PAPER-1 전 Demo 전환 재결정 |
| Q3 (U-14) | 24/7 시장의 일일 기준 시각: UTC 00:00(=KST 09:00, 1d 봉 경계와 일치) / KST 00:00 / 기타 | **결정 A-055**: UTC 00:00 (= KST 09:00, 1d 봉 경계와 일치) |
| Q4 | API 키 유형: Ed25519(문서 권장, WebSocket API 사용자 스트림 subscribe 경로) / HMAC(실전은 IP 제한 필수) | **결정 A-063⑨**: 테스트넷 HMAC + signature 방식 구독(B-U9 전제), 실전 키는 LIVE 준비 때 재결정 |
| Q5 | 테스트넷 리셋 발생 시 PAPER 기록 처리: 즉시 정지 후 사람 확인, 리셋 이전·이후 기간을 PAPER 평가 기간에서 어떻게 셀지 | **결정 A-062①**: 정지·사람 확인, PAPER 기간은 리셋 시점부터 다시 계산 |
