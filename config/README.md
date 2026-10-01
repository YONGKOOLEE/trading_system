# config/ — 비민감 설정

- 비밀정보(앱키·시크릿·토큰·계좌번호)는 두지 않는다. 비밀정보는 `.env`(git 제외)에 둔다.
- `strategies/` — 전략 파라미터 파일(R-19).
- `limits.<mode>.toml` — 계좌 한도·시간 설정 (01 D-026). 값은 **사용자가 정한다**. 비민감 설정이라 버전 관리 대상이지만(03 D-058) 저장소에 기본값을 두지 않는다. 형식 예제 `limits.example.toml`은 자리표시자라 그대로는 시작할 수 없다. 로더: `trading_system.config.limits_file`.
- `calendar/krx-<연도>.toml` — 시장 달력 (03 D-055, T-010).
- `ticks/krx.toml` — 호가 단위 (03 D-056, T-010).
- 형식은 03-design §13·D-058(TOML)을 따른다.
