from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class PerformanceReportInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    cycle: str = Field(min_length=1, max_length=20)
    dimension: Literal["department", "level", "tenure"] = "department"


class ReminderSendInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    review_ids: list[int] | None = Field(
        default=None,
        max_length=100,
    )
