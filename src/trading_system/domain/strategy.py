"""전략 명세·판정 입력·상태 (00 §2.2~§2.11, 02 ENT-05~07·ENT-10~12). 표준 라이브러리만.

전략 코드는 이 모듈의 값 객체만 받고 신호(models.Signal)만 낸다 (00 D-002, D-005).
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from .clock import require_aware
from .enums import RequestType
from .investor_flow import InvestorFlow
from .market import TradingStatus

# 같은 판정·같은 종목에서 규칙 종류 간 순서. 위험 축소가 항상 앞선다 (00 D-006, 전략이 바꿀 수 없음)
KIND_ORDER: tuple[RequestType, ...] = (RequestType.EXIT, RequestType.REDUCE, RequestType.INCREASE, RequestType.ENTRY)
RULE_KINDS = frozenset({"ENTRY", "INCREASE", "REDUCE", "EXIT", "REENTRY_BLOCK"})
SIZING_METHODS = frozenset({"S-1", "S-2", "S-3", "S-4"})
UNIVERSE_METHODS = frozenset({"U-A", "U-B", "U-C"})
MARKETS = frozenset({"M1", "M2", "M3"})


class StrategyState(StrEnum):
    """00 §2.11, 02 ENT-06."""

    DRAFT = "DRAFT"                      # 초안
    BACKTEST_PASSED = "BACKTEST_PASSED"  # 백테스트 통과 (이 시스템 백테스트 실행 기록, A-029)
    PAPER = "PAPER"                      # PAPER 중
    LIVE_CANDIDATE = "LIVE_CANDIDATE"    # LIVE 후보
    LIVE = "LIVE"                        # LIVE 중
    STOPPED = "STOPPED"                  # 중지
    RETIRED = "RETIRED"                  # 폐기


class StopReason(StrEnum):
    REPLACE = "REPLACE"  # 교체 (01 K9: 해당 전략의 감액·청산 신호 허용)
    USER = "USER"        # 사용자 판단 (운영 요청만)


# 허용 전이 (00 §2.11 표). STOPPED → 중지 전 상태, 모든 상태 → RETIRED
ALLOWED_TRANSITIONS: dict[StrategyState, frozenset[StrategyState]] = {
    StrategyState.DRAFT: frozenset({StrategyState.BACKTEST_PASSED, StrategyState.RETIRED}),
    StrategyState.BACKTEST_PASSED: frozenset({StrategyState.PAPER, StrategyState.RETIRED}),
    StrategyState.PAPER: frozenset({StrategyState.LIVE_CANDIDATE, StrategyState.STOPPED, StrategyState.RETIRED}),
    StrategyState.LIVE_CANDIDATE: frozenset({StrategyState.LIVE, StrategyState.STOPPED, StrategyState.RETIRED}),
    StrategyState.LIVE: frozenset({StrategyState.STOPPED, StrategyState.RETIRED}),
    StrategyState.STOPPED: frozenset({StrategyState.PAPER, StrategyState.LIVE_CANDIDATE, StrategyState.LIVE,
                                      StrategyState.RETIRED}),
    StrategyState.RETIRED: frozenset(),
}


@dataclass(frozen=True)
class ParamSpec:
    """파라미터 한 개의 스키마 (00 §2.4, R-19). 값은 config/strategies/에 둔다."""

    name: str
    type: str                     # "decimal" / "int" / "str" / "bool"
    unit: str
    description: str
    minimum: Decimal | None = None
    maximum: Decimal | None = None
    choices: tuple[str, ...] = ()


@dataclass(frozen=True)
class RuleSpec:
    """규칙 표 한 행 (00 §2.2)."""

    rule_id: str
    kind: str                     # ENTRY / INCREASE / REDUCE / EXIT / REENTRY_BLOCK
    inputs: tuple[str, ...]       # CAP-ID 또는 "상태 입력" 표기
    trigger: str                  # CAP-TRG-xx
    condition: str
    action: str
    invalid_when: str
    priority: int                 # 같은 종류 규칙끼리의 순위 (작을수록 먼저)


@dataclass(frozen=True)
class StrategyLimits:
    """전략별 한도 (01 §2.3.1-8, E-10). 비우면 계좌 한도만. 모두 계좌 한도 이하여야 등록된다."""

    l1_max_loss_ratio: Decimal | None = None
    l2_max_symbol_ratio: Decimal | None = None
    l3_max_total_ratio: Decimal | None = None
    l7_max_order_amount: Decimal | None = None


@dataclass(frozen=True)
class StrategySpec:
    """등록 명세 (00 §2.4). 모든 항목 필수."""

    strategy_id: str
    name: str
    description: str
    version: int
    markets: tuple[str, ...]
    universe_method: str
    universe: tuple[str, ...]          # U-A면 종목 목록, U-B면 목록 ID
    capabilities: tuple[str, ...]
    data_requirements: str             # 종류·주기·워밍업 구간·수정주가 여부
    trigger: str                       # 판정 시점 (트리거와 시장 현지 시각)
    rules: tuple[RuleSpec, ...]
    params: tuple[ParamSpec, ...]
    signal_format: str                 # §2.5를 따름 + 추가 근거 필드
    validity_rule: str                 # 기한 산정 규칙 설명
    validity_max_minutes: int          # 절대 상한 (판정 기준 시각부터, D-007)
    unfilled_policy: str               # CAP-ORD-03 선언 시 필수, 아니면 "시스템 기본"
    sizing: str                        # S-1~S-4
    holding: str                       # 예상 보유 기간·최대 보유 제한
    limits: StrategyLimits = field(default_factory=StrategyLimits)
    universe_list: str | None = None   # [CP-034 D-198] U-B 목록 ID (U-B 필수, 그 밖에는 없음)
    kind: str = "STRATEGY"             # [CP-034 D-199] STRATEGY | ALLOCATION(배분 규칙 — 등록·상태 전환은 전략과 같다)
    allocation_targets: tuple[str, ...] = ()   # [CP-034 D-199] 배분 규칙이 신규 진입을 허용·불허하는 전략 ID

    @property
    def owner(self) -> str:
        return f"{self.strategy_id}:{self.version}"

    def spec_hash(self) -> str:
        """명세 해시 (02 ENT-05). 파라미터 값은 포함하지 않는다(값은 설정 버전).

        CP-034에서 더한 선택 항목은 기본값이면 해시에 넣지 않는다 — 기존 전략(예: paper0-verify v2)의 해시가 바뀌면
        등록 일치 검사(D-011)가 이미 등록된 버전을 거부하기 때문이다."""
        values = asdict(self)
        for name, default in _HASH_OPTIONAL_DEFAULTS.items():
            if values.get(name) == default:
                values.pop(name, None)
        text = json.dumps(values, sort_keys=True, ensure_ascii=False, default=str, separators=(",", ":"))
        return hashlib.sha256(text.encode("utf-8")).hexdigest()


# 기본값이면 명세 해시에서 빼는 항목 (CP-034 — 기존 명세 해시 불변)
_HASH_OPTIONAL_DEFAULTS: dict[str, object] = {"universe_list": None, "kind": "STRATEGY", "allocation_targets": ()}


def evaluation_id_of(strategy_id: str, version: int, market: str, trigger_key: str) -> str:
    """결정적 판정 ID (00 §2.5, 02 ENT-10). 시각·주기 트리거는 예정 판정 시각, 이벤트 트리거는 이벤트 식별자."""
    key = "\x1f".join([strategy_id, str(version), market, trigger_key])
    return "ev-" + hashlib.sha256(key.encode("utf-8")).hexdigest()[:32]


# --- 판정 입력 (읽기 전용 스냅샷) ---------------------------------------------------------

@dataclass(frozen=True)
class Bar:
    """봉 하나. 일봉은 date, 분봉은 시작 시각(aware)."""

    at: date | datetime
    open: Decimal
    high: Decimal
    low: Decimal
    close: Decimal
    volume: Decimal
    # [CP-040 D-260] 분봉 확장 열 (바이낸스 klines 7~10번째 값). 없으면 None — 일봉·M1 분봉·확장 열이 없는 봉
    quote_volume: Decimal | None = None
    trades: int | None = None
    taker_buy_base: Decimal | None = None
    taker_buy_quote: Decimal | None = None


@dataclass(frozen=True)
class PositionView:
    quantity: Decimal
    avg_price: Decimal
    holding_days: int        # 보유 거래일 수
    owned_by_me: bool        # 이 전략(버전)이 소유


@dataclass(frozen=True)
class InstrumentView:
    """한 종목에 대한 판정 입력. 품질 검사를 통과한 데이터만 담긴다 (R-22)."""

    instrument: str
    daily_bars: tuple[Bar, ...] = ()
    minute_bars: tuple[Bar, ...] = ()
    last_price: Decimal | None = None
    statuses: frozenset[TradingStatus] = frozenset()
    position: PositionView | None = None
    has_pending_order: bool = False
    last_exit_at: datetime | None = None
    last_exit_reason: str | None = None
    entries_recent: int = 0
    data_ok: bool = True     # 품질 실패면 False → 전략은 신호를 내지 않음
    listed_at: datetime | None = None   # [CP-034 D-198] U-B 목록 연속 등재 시작 버전의 제출 시각(KST). U-A는 None
    # [CP-033 D-189·D-193] CAP-DAT-05a 선언 전략만: 일자 < 판정일·확정값만, 일자 오름차순. 미선언 전략은 언제나 ()
    investor_flows: tuple[InvestorFlow, ...] = ()
    # [CP-037 DP-78] CAP-DAT-02 선언 전략의 1분봉 판정 입력이 유효한지. False면 minute_bars는 ()이고, 판정 실행기가 이 종목의
    # 신규 진입·증액 신호를 버린다 — 청산·감액(보유·최근가 등 나머지 입력 기반)은 막지 않는다(CLAUDE.md §8-3). 미선언 전략은 True
    minute_ok: bool = True
    # [CP-040 D-259] 집계 봉(15분·60분, 저장 1분 봉에서 UTC 정렬로 계산) 판정 창 — 집계 봉을 선언한 CAP-DAT-02 전략만.
    # 결측 집계 봉은 빼고(채우지 않음) 시작 시각 오름차순, 그 시작 시각은 agg_missing(오름차순)에. minute_ok False면 둘 다 ()
    agg_bars: tuple[Bar, ...] = ()
    agg_missing: tuple[datetime, ...] = ()


@dataclass(frozen=True)
class EvaluationSnapshot:
    """판정 한 번의 입력 (00 §2.2 판정 규칙). 판정 기준 시각 이후 데이터는 들어 있지 않다 (R-10)."""

    evaluation_id: str
    market: str
    account: str
    as_of: datetime
    base_amount: Decimal                      # S-2 분모 (01 §2.4 기준 금액)
    instruments: tuple[InstrumentView, ...]
    next_evaluation_at: datetime | None = None  # 시각·주기 트리거의 다음 예정 판정 (기한 상한, 00 §2.6)

    def __post_init__(self) -> None:
        require_aware(self.as_of)
        if self.next_evaluation_at is not None:
            require_aware(self.next_evaluation_at)

    def view(self, instrument: str) -> InstrumentView | None:
        return next((v for v in self.instruments if v.instrument == instrument), None)
