
"""
集中管理状态、转换规则、财务时间解析和工作日计算
"""
from datetime import datetime, UTC, timedelta, time

from app.utils.time import SHANGHAI_TIMEZONE, as_shanghai

POLL_STATUSES = {
    "submitted", "manager_approved", "finance_approved"
}

REJECTED_STATUSES = {
    "rejected", "manager_rejected", "finance_rejected"
}

TERMINAL_STATUSES = REJECTED_STATUSES | {"paid"}


STATUS_LABELS = {
    "draft": "待确认",
    "submitting": "正在提交财务",
    "submitted": "已提交，待主管审批",
    "manager_approved": "主管审批通过，待财务复核",
    "finance_approved": "财务复核通过，待出纳打款",
    "rejected": "已拒绝",
    "paid": "已打款",
    "cancelled": "已取消",
    "approved": "历史状态已审批",
    "manager_rejected": "主管已退回",
    "finance_rejected": "财务已退回",
}

TRANSITIONS = {
    "submitted": {
        "manager_approved", "manager_rejected", "rejected",
    },
    "manager_approved": {
        "finance_approved", "finance_rejected", "rejected",
    },
    "finance_approved": {"paid", "rejected"},
}

REJECTION_SUGGESTIONS = {
    ("金额超标", "请替换超标发票，或按现有规则申请超标报销"),
    ("抬头错误", "请联系开票方重开发票，抬头应为公司全称"),
    ("发票重复", "请检查是否重复上传或重复报销同一张发票"),
    ("日期不符", "请核对发票日期和出差日期后重新提交")
}

def rejection_suggestion(reason: str) -> str:
    suggestions = [
        suggestion
        for keyword, suggestion in REJECTION_SUGGESTIONS
        if keyword in reason
    ]

    return "".join(suggestions) or "请根据审批意见修改后提交"

def ensure_transition(old: str, new: str) -> None:
    if new not in TRANSITIONS.get(old, set()):
        raise ValueError(f"不支持的审批状态流转:{old} -> {new}")

def parse_finance_time(value: str) -> datetime:
    if not isinstance(value, str):
        raise ValueError("财务时间必须是 ISO 8601 字符串")
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("财务时间必须包含时区")
    return parsed.astimezone(UTC).replace(tzinfo=None)

def working_seconds(start, end, calendar) -> float:
    start = as_shanghai(start)
    end = as_shanghai(end)
    if end <= start:
        return 0.0

    seconds = 0.0
    day = start.date()
    while day <= end.date():
        if day not in calendar:
            raise ValueError("工作日历不完整")
        if calendar[day]:
            left = datetime.combine(
                day, time.min, SHANGHAI_TIMEZONE,
            )
            right = left + timedelta(days=1)
            seconds += max(
                0.0,
                (min(end, right) - max(start, left)).total_seconds(),
            )
        day += timedelta(days=1)
    return seconds


