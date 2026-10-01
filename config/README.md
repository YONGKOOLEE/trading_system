# config/ — 비민감 설정

- 비밀정보(앱키·시크릿·토큰·계좌번호)는 두지 않는다. 비밀정보는 `.env`(git 제외)에 둔다.
- `strategies/` — 전략 파라미터 파일(R-19).
- `limits.<mode>.toml` — 계좌 한도·시간 설정 (01 D-026, T-002).
- `calendar/krx-<연도>.toml` — 시장 달력 (03 D-055, T-010).
- `ticks/krx.toml` — 호가 단위 (03 D-056, T-010).
- 형식은 03-design §13·D-058(TOML)을 따른다.
