from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

RuleType = Literal["reimbursement", "approval", "attendance"]


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class DocBindingInput(StrictModel):
    doc_id: str = Field(min_length=1, max_length=128, pattern=r"^[A-Za-z0-9_-]+$")
    doc_title: str = Field(default="", max_length=256)
    acknowledged_revision: int = Field(ge=1, strict=True)


class RuleUpdate(StrictModel):
    expected_version: int = Field(ge=0, strict=True)
    rule_data: dict[str, Any]
    change_summary: str = Field(min_length=1, max_length=512, pattern=r"\S")
    bindings: list[DocBindingInput] = Field(default_factory=list, max_length=50)


class RulePreview(RuleUpdate):
    samples: list[dict] = Field(default_factory=list, max_length=20)


class RuleCandidate(StrictModel):
    update: RuleUpdate | None = None
    rollback_version: int | None = Field(default=None, ge=1)
    expected_version: int = Field(ge=0)
    change_summary: str = Field(min_length=1, max_length=512, pattern=r"\S")


class RuleConfirmation(StrictModel):
    change_id: str = Field(pattern=r"^[a-f0-9]{32}$")
    token: str = Field(min_length=32, max_length=128)


class RuleBindingsUpdate(StrictModel):
    expected_version: int = Field(ge=1)
    change_summary: str = Field(min_length=1, max_length=512, pattern=r"\S")
    bindings: list[DocBindingInput] = Field(max_length=50)
