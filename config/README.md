# config/ — 비민감 설정

- 비밀정보(앱키·시크릿·토큰·계좌번호)는 두지 않는다. 비밀정보는 `.env`(git 제외)에 둔다.
- `strategies/` — 전략 파라미터 파일(R-19).
- `limits.<mode>.toml` — 계좌 한도·시간 설정 (01 D-026). 값은 **사용자가 정한다**. 비민감 설정이라 버전 관리 대상이지만(03 D-058) 저장소에 기본값을 두지 않는다. 형식 예제 `limits.example.toml`은 자리표시자라 그대로는 시작할 수 없다. 로더: `trading_system.config.limits_file`.
- `market.<mode>.toml` — 시세 신선도·가격제한폭·비용 요율 (03 D-095·D-065). 예제 `market.example.toml`(자리표시자). 로더: `trading_system.config.market_file`.
- `calendar/krx-<연도>.toml` — 시장 달력 (03 D-055). 실제 휴장일은 사용자가 KRX 공지로 작성(X-U3). 예제 `calendar/krx-YYYY.example.toml`. 파일이 없는 연도는 모든 주문 거부.
- `ticks/krx.toml` — 호가 단위 (03 D-056, CP-032 D-173). X-U1 확인(2026-10-08, krx-market §2.1) 값. 파일이 없으면 지정가 거부, 시장가만. 예제 `ticks/krx.example.toml`. PAPER 지정가 사용은 모의 호가 격자 실측(Q-3) 뒤.
- 형식은 03-design §13·D-058(TOML)을 따른다.
