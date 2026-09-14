from dataclasses import Field
from datetime import date
from typing import Literal

from attr.validators import min_len
from pydantic import BaseModel, ConfigDict, Field, model_validator



class LeaveInput(BaseModel):
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    leave_type: Literal["annual", "compensatory", "sick", "personal"]
    start_date: date
    end_date: date
    reason: str = Field(min_length=1,max_length=1024)

    @model_validator(mode="after")
    def validate_period(self):
        if self.end_date < self.start_date:
            raise ValueError("结束日期不能早于开始日期")
        if (self.end_date - self.start_date).days > 366:
            raise ValueError("单次申请日期跨度不能超过366天")
        return self

class DraftAction(BaseModel):
    model_config = ConfigDict(extra="forbid",str_strip_whitespace=True)

    draft_id: str = Field(pattern=r"^[a-f0-9]{32}$")

class ApprovalInput(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)
    action: Literal["approve", "reject"]
    reject_reason: str = Field(default="", max_length=512)

    @model_validator(mode="after")
    def validate_rejection(self):
        if self.action == "reject" and not self.reject_reason:
            raise ValueError("拒绝申请必须填写原因")
        if self.action == "approve" and self.reject_reason:
            raise ValueError("通过申请不应填写拒绝原因")
        return self


