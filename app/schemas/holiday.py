from datetime import datetime, time, timedelta, date
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.settings import get_settings
from app.utils.time import SHANGHAI_TIMEZONE


class DutyInput(BaseModel):
    """
        员工值班安排
    """
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    user_id: int = Field(gt=0)

    duty_date: date
    duty_time: str = Field(pattern=r"^(?:[01]\d|2[0-3]):[0-5]\d-"
                r"(?:[01]\d|2[0-3]):[0-5]\d$",)
    duty_notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def validate_time(self):
        start, end = self.duty_time.split("-")

        if end <= start:
            raise ValueError("值班时间必须晚于开始时间")

        return self

class NoticeInput(BaseModel):
    """"
        接收创建放假通知的数据
    """
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    title: str = Field(default=None, min_length=1, max_length=200)
    holiday_name: str = Field(min_length=1, max_length=50)
    start_date: date
    end_date: date
    workday_arrangement: str | None = None
    advance_days: int = Field(default_factory=lambda: get_settings().holiday_advance_days)
    duty_schedule: list[DutyInput] = Field(default_factory=list)
    deadline: datetime | None = None

    @model_validator(mode="after")
    def validate_notice(self):
        if self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期")

        if self.title is None:
            self.title = (
                f"{self.start_date.year}年"
                f"{self.holiday_name}放假通知"
            )

        seen = set()
        for duty in self.duty_schedule:
            if not self.start_date <= duty.duty_date <= self.end_date:
                raise ValueError("值班日期必须位于假期范围内")

            identity = (
                duty.user_id, duty.duty_date, duty.duty_time,
            )
            if identity in seen:
                raise ValueError("值班安排存在重复记录")
            seen.add(identity)

        if self.deadline is None:
            self.deadline = datetime.combine(
                self.start_date - timedelta(days=1),
                time(get_settings().holiday_deadline_hour),
                SHANGHAI_TIMEZONE,
            )
        elif self.deadline.utcoffset() is None:
            raise ValueError("deadline 必须带时区")

        self.deadline = self.deadline.astimezone(
            SHANGHAI_TIMEZONE,
        )

        holiday_start = datetime.combine(
            self.start_date,
            time.min,
            SHANGHAI_TIMEZONE,
        )
        if self.deadline >= holiday_start:
            raise ValueError("确认截止时间必须早于假期开始")

        return self

class NoticeUpdate(NoticeInput):
    """
        接收修改放假通知的数据
    """
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    status: Literal["draft", "published", "cancelled"] = "draft"

class CronPayload(BaseModel):
    """
        定时任务执行时需要的业务参数，推送哪一条通知，每一批次推送多少条，每一批次间隔时间
    """
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    notice_id: int = Field(gt=0)
    batch_size: int = Field(default=50,ge=1,le=50)
    batch_interval_sec: float = Field(default=1.1, ge=1.1)


class CronInput(BaseModel):
    """"
        接收定时任务完整请求
    """
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    task_type: Literal["HOLIDAY_NOTICE_PUSH"]
    payload: CronPayload
    execute_at: datetime
    max_retries: int = Field(default=3, ge=0,le=3)

    @model_validator(mode="after")
    def validate_timezone(self):
        if self.execute_at.utcoffset() is None:
            raise ValueError("execute_at 必须带时区")
        return self

class TextReceiptInput(BaseModel):
    """
        接收员工通过文字确认通知的数据
    """
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    text: str = Field(min_length=1,max_length=100)
    notice_id: int | None = Field(default=None, ge=0)

