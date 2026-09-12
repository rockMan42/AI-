
from datetime import datetime, date
from typing import Literal
from zoneinfo import ZoneInfo

from pydantic import BaseModel, ConfigDict, Field, model_validator

SHANGHAI = ZoneInfo("Asia/Shanghai")

QueryType = Literal["punch_record", "late_count", "leave_balance", "all"]

STATUS_VALUES = {
    "normal": ("normal", "正常"),
    "late": ("late", "迟到"),
    "early_leave": ("early_leave", "早退"),
    "absent": ("absent", "缺勤"),
    "leave": ("leave", "请假"),
}

def now_shanghai() -> datetime:
    return datetime.now(SHANGHAI)

def month_bounds(month: str) -> tuple[date, date]:
    year, month = map(int, month.split("-"))

    start = date(year, month, 1)

    if month == 12:
        end = date(year + 1, 1, 1)
    else:
        end = date(year, month + 1, 1)

    return start, end

class AttendanceQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: str
    query_type: QueryType = "all"
    month: str | None = Field(
        default=None,
        pattern=r"^[0-9]{4}-(0[1-9]|1[0-2])$",
    )
    year: int | None = Field(
    default=None,
    ge=1900,
    le=9999,
    description="假期余额所属年度，不填默认当前年度",
)

    # 保留项目已有的按日查询和状态筛选。
    query_date: date | None = None
    status_filter: str | None = None

    limit: int = Field(default=5, ge=1, le=100)
    offset: int = Field(default=0, ge=0)

    @model_validator(mode="after")
    def normalize(self):
        if int(self.user_id) > 9223372036854775807:
            raise ValueError("用户 ID 超出支持范围")

        if self.month is None:
            source_date = self.query_date or now_shanghai().date()
            self.month = source_date.strftime("%Y-%m")

        # 同时校验年份和下个月边界是否合法。
        month_bounds(self.month)

        if (
                self.query_date is not None
                and self.query_date.strftime("%Y-%m") != self.month
        ):
            raise ValueError("查询日期与查询月份不一致")

        if self.status_filter is not None:
            for canonical, aliases in STATUS_VALUES.items():
                if self.status_filter in aliases:
                    self.status_filter = canonical
                    break
            else:
                raise ValueError("不支持的考勤状态")

        return self

class LateStats(BaseModel):
    model_config = ConfigDict(extra="forbid")

    late_count: int = Field(ge=0, strict=True)
    early_leave_count: int = Field(ge=0, strict=True)
    record_count: int = Field(ge=0, strict=True)
    calculated_at: datetime