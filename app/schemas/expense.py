from datetime import date
from decimal import Decimal
from typing import Literal, Any

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


class StrictModel(BaseModel):
    model_config = ConfigDict(
        extra="forbid",
        from_attributes=True,
    )

class ExpenseSupplement(StrictModel):
    expense_type: Literal[
        "hotel", "meal", "train", "flight", "taxi", "other",
    ]
    city: str | None = Field(default=None, max_length=100)
    check_in: date | None = None
    check_out: date | None = None
    seat: str | None = Field(default=None, max_length=50)

    @model_validator(mode="after")
    def check_business_fields(self):
        if self.expense_type == "hotel":
            if not self.city or not self.check_in or not self.check_out:
                raise ValueError("住宿必须填写城市、入住日期和退房日期")
            if self.check_out <= self.check_in:
                raise ValueError("退房日期必须晚于入住日期")
        if self.expense_type in {"train", "flight"} and not self.seat:
            raise ValueError("火车和飞机必须填写座席或舱位")
        return self


class ExpenseCheckInput(StrictModel):
    trip_id: int | None = Field(default=None, gt=0)
    invoice_ids: list[int] = Field(min_length=1, max_length=100)

    @model_validator(mode="after")
    def check_invoice_ids(self):
        if any(value <= 0 for value in self.invoice_ids):
            raise ValueError("发票ID必须为正整数")
        if len(set(self.invoice_ids)) != len(self.invoice_ids):
            raise ValueError("不能重复选择同一发票")
        return self

class ExpensePrepareInput(ExpenseCheckInput):
    accept_overrides: bool = False
    override_reason: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def check_override(self):
        self.override_reason = self.override_reason.strip()
        if self.accept_overrides and not self.override_reason:
            raise ValueError("申请超标报销必须填写原因")
        return self

class ExpenseSubmitInput(StrictModel):
    expense_id: int = Field(gt=0)

NUMERIC_FIELDS = {
    "amount", "nightly_amount", "meal_daily_amount",
}
TEXT_FIELDS = {
    "buyer_name", "buyer_tax_id", "invoice_date",
    "city", "seat", "position_level",
}
RULE_FIELDS = NUMERIC_FIELDS | TEXT_FIELDS

REFERENCE_FIELDS = {
    "$company_name", "$company_tax_id",
    "$trip_window", "$max_amount",
}

class RuleCondition(StrictModel):
    field: str
    operator: Literal[
        "lte", "gte", "lt", "gt", "eq",
        "in", "between", "contains", "not_contains",
        "date_between",
    ]
    value: Any

    @model_validator(mode="after")
    def validate_expression(self):
        if self.field not in RULE_FIELDS:
            raise ValueError("规则字段不在白名单中")

        value = self.value
        if isinstance(value, str) and value.startswith("$"):
            if value not in REFERENCE_FIELDS:
                raise ValueError("不支持的上下文引用")
            return self

        if self.operator in {"between", "date_between"}:
            if not isinstance(value, list) or len(value) != 2:
                raise ValueError("范围条件必须提供两个边界")
        elif self.operator == "in":
            if not isinstance(value, list) or not value:
                raise ValueError("in 条件必须提供非空列表")

        return self

class ExpenseRuleInput(StrictModel):
    rule_name: str = Field(min_length=1, max_length=100)
    rule_category: Literal[
        "all", "accommodation", "meals", "transport", "other",
    ]
    city_tier: Literal["tier1", "tier2", "all"] = "all"
    position_level: Literal[
        "director", "manager", "supervisor", "employee", "all",
    ] = "all"
    expense_type: Literal[
        "all", "hotel", "meal", "train", "flight", "taxi", "other",
    ]
    max_amount: Decimal | None = Field(
        default=None, ge=0, max_digits=12, decimal_places=2,
    )
    effective_date: date
    expiry_date: date | None = None
    status: Literal["active", "inactive"] = "active"
    version: str = Field(default="1.0", max_length=20)
    scope: Literal["invoice", "trip"] = "invoice"
    conflict_group: str = Field(min_length=1, max_length=50)
    priority: int = Field(default=0, ge=0, le=10000)
    condition_json: RuleCondition
    error_message: str = Field(min_length=1, max_length=500)
    suggestion: str = Field(default="请核实费用信息", max_length=500)
    severity: Literal["block", "warn"] = "block"
    allow_override: bool = False

    @field_validator("rule_category", mode="before")
    @classmethod
    def normalize_category(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip().lower()
        return {
            "transportation": "transport",
            "交通": "transport",
            "住宿": "accommodation",
            "餐饮": "meals",
            "餐食": "meals",
            "其他": "other",
            "全部": "all",
        }.get(value, value)

    @field_validator("expense_type", mode="before")
    @classmethod
    def normalize_expense_type(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip().lower()
        return {
            "酒店": "hotel",
            "住宿": "hotel",
            "餐饮": "meal",
            "餐食": "meal",
            "餐费": "meal",
            "火车": "train",
            "火车票": "train",
            "飞机": "flight",
            "机票": "flight",
            "出租车": "taxi",
            "其他": "other",
            "全部": "all",
        }.get(value, value)

    @field_validator("position_level", mode="before")
    @classmethod
    def normalize_position_level(cls, value):
        if not isinstance(value, str):
            return value
        value = value.strip().lower()
        return {
            "经理": "manager",
            "主管": "supervisor",
            "员工": "employee",
            "普通员工": "employee",
            "总监": "director",
            "全部": "all",
        }.get(value, value)

    @model_validator(mode="after")
    def validate_rule(self):
        if self.expiry_date and self.expiry_date < self.effective_date:
            raise ValueError("失效日期不能早于生效日期")

        condition = self.condition_json
        if condition.value == "$max_amount" and self.max_amount is None:
            raise ValueError("使用额度引用时必须填写 max_amount")

        if condition.field in {
            "buyer_name", "buyer_tax_id", "invoice_date",
        }:
            if self.allow_override or self.severity != "block":
                raise ValueError("抬头、税号、日期规则必须阻断且不可例外")

        return self


class ExpensePolicyInput(StrictModel):
    company_name: str = Field(min_length=1, max_length=200)
    company_tax_id: str = Field(min_length=1, max_length=50)
    tier1_cities: list[str] = Field(
        default_factory=lambda: ["北京", "上海", "广州", "深圳"],
    )
    position_map: dict[str, Literal[
        "director", "manager", "supervisor", "employee",
    ]] = Field(default_factory=dict)
    date_tolerance_days: int = Field(default=1, ge=0, le=7)
