"""한도 설정 테스트용 가상 값. 승인된 리스크 수치가 아니며 저장소의 기본값이 아니다."""

from __future__ import annotations

VALID = {
    "account": "KR-BT-01",
    "limits": {
        "l1_max_loss_ratio": "0.01",
        "l2_max_symbol_ratio": "0.20",
        "l3_max_total_ratio": "0.50",
        "l4_daily_loss_ratio": "0.02",
        "l5_daily_new_orders": 10,
        "l6_max_positions": 5,
        "l7_max_order_amount": "1000000",
        "l10_leverage_total_ratio": "none",
        "warn_usage_ratio": "0.80",
    },
    "time": {
        "order_window_start": "09:05",
        "order_window_end": "15:15",
        "daily_reference_time": "08:00",
        "report_time": "16:00",
    },
}


TOML = """
account = "KR-BT-01"

[limits]
l1_max_loss_ratio = "0.01"
l2_max_symbol_ratio = "0.20"
l3_max_total_ratio = "0.50"
l4_daily_loss_ratio = "0.02"
l5_daily_new_orders = 10
l6_max_positions = 5
l7_max_order_amount = "1000000"
l10_leverage_total_ratio = "none"
warn_usage_ratio = "0.80"

[time]
order_window_start = "09:05"
order_window_end = "15:15"
daily_reference_time = "08:00"
report_time = "16:00"
"""
