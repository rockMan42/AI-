from datetime import date, timedelta, datetime
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.utils.time import as_shanghai, utc_now, SHANGHAI_TIMEZONE

STATUS_LABELS = {
    "new": "新线索",
    "contacted": "已联系",
    "qualified": "已确认",
    "proposal": "方案沟通",
    "won": "已成交",
    "lost": "已丢失",
}

Priority = Literal["high", "medium", "low"]
LeadView = Literal["all", "overdue", "high_intent", "due_soon"]

class LeadQuery(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    assigned_to: int | None = Field(default=None, gt=0)
    team: bool = False
    status: str | None = None
    priority: Priority | None = None
    source: Literal[
        "website", "referral", "exhibition", "cold_call"
    ] | None = None
    company_name: str | None = Field(default=None, max_length=200)
    date_from: date | None = None
    date_to: date | None = None
    last_follow_before: date | None = None
    time_range: Literal[
        "今天", "本周", "上周", "本月", "最近7天"
    ] | None = None
    view: LeadView = "all"
    sort_by: Literal["default", "priority_score"] = "default"
    page: int = Field(default=1, ge=1)
    page_size: int = Field(default=10, ge=1, le=20)

    @field_validator("status")
    @classmethod
    def validate_status(cls, value):
        if value is None:
            return None
        values = list(dict.fromkeys(
            item.strip() for item in value.split(",")
        ))
        if not values or any(item not in STATUS_LABELS for item in values):
            raise ValueError("线索状态无效")
        return ",".join(values)

    @model_validator(mode="after")
    def resolve_dates(self):
        if self.time_range:
            if self.date_from is not None or self.date_to is not None:
                raise ValueError("时间范围与起止日期不能同时填写")

            today = as_shanghai(utc_now()).date()
            monday = today - timedelta(days=today.weekday())
            ranges = {
                "今天": (today, today),
                "本周": (monday, today),
                "上周": (
                    monday - timedelta(days=7),
                    monday - timedelta(days=1),
                ),
                "本月": (today.replace(day=1), today),
                "最近7天": (today - timedelta(days=6), today),
            }
            self.date_from, self.date_to = ranges[self.time_range]
            self.time_range = None

        if self.date_from and self.date_to and self.date_from > self.date_to:
            raise ValueError("开始日期不能晚于结束日期")
        return self


class FollowUpInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    # 兼容文档请求体；真实跟进人始终取认证身份。
    user_id: int | None = Field(default=None, gt=0)
    follow_up_type: Literal["phone", "email", "visit", "wechat", "demo"]
    content: str = Field(min_length=1, max_length=2000)
    outcome: Literal[
        "positive", "neutral", "negative", "no_answer"
    ] | None = None
    next_action: str | None = Field(default=None, max_length=200)
    next_follow_up: datetime | None = None

    @field_validator("next_follow_up", mode="before")
    @classmethod
    def normalize_time(cls, value):
        if value in (None, ""):
            return None
        if isinstance(value, str):
            value = datetime.fromisoformat(value.replace("Z", "+00:00"))
        if value.tzinfo is None:
            value = value.replace(tzinfo=SHANGHAI_TIMEZONE)
        return value


