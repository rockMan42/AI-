from pydantic import BaseModel, Field


class CategoryRuleInput(BaseModel):
    field_name: str = Field(min_length=1, max_length=50)
    display_name: str = Field(min_length=1, max_length=50)
    required: bool = False
    default_value: str | int | None = None
    validation_rule: str | None = Field(
        default=None,
        max_length=200,
    )
    sort_order: int = Field(default=0, ge=0)


class CategoryRuleReplaceInput(BaseModel):
    fields: list[CategoryRuleInput] = Field(
        min_length=1,
        max_length=20,
    )

from typing import Literal


class MockStatusInput(BaseModel):
    status: Literal[
        "pending",
        "approving",
        "approved",
        "rejected",
        "fulfilled",
    ]
    approver_id: int | None = Field(default=None, gt=0)
    action: Literal["approve", "reject"] | None = None
    comment: str | None = Field(
        default=None,
        max_length=500,
    )

