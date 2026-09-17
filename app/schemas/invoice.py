from typing import Annotated, Literal, Any

from pydantic import Field, ConfigDict, BaseModel

RequestID = Annotated[str, Field(pattern=r"^[A-Za-z0-9_-]{1,64}$")]

FIELD_LABELS = {
    "invoice_type": "发票类型",
    "invoice_code": "发票代码",
    "invoice_number": "发票号码",
    "invoice_date": "开票日期",
    "total_amount": "价税合计",
    "amount_without_tax": "不含税金额",
    "tax_amount": "税额",
    "buyer_name": "购买方名称",
    "buyer_tax_id": "购买方税号",
    "seller_name": "销售方名称",
    "items_description": "商品或服务",
    "expense_city": "费用发生城市",
    "service_start_date": "服务开始日期",
    "service_end_date": "服务结束日期",
    "transport_seat": "交通座席/舱位",
}

class InvoiceFields(BaseModel):
    invoice_type: str | None = None
    invoice_code: str | None = None
    invoice_number: str | None = None
    invoice_date: str | None = None
    total_amount: float | None = None
    amount_without_tax: float | None = None
    tax_amount: float | None = None
    buyer_name: str | None = None
    buyer_tax_id: str | None = None
    seller_name: str | None = None
    items_description: str | None = None
    expense_city: str | None = None
    service_start_date: str | None = None
    service_end_date: str | None = None
    transport_seat: str | None = None

class InvoiceOCRResult(InvoiceFields):
    confidence: dict[str, float] = Field(default_factory=dict)
    needs_confirm: bool = True
    low_confidence_fields: list[str] = Field(default_factory=list)
    raw_text: str | None = None
    error: str | None = None

class InvoiceOCRInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    user_id: int = Field(gt=0)
    request_id: RequestID
    image_urls: list[
        Annotated[str, Field(max_length=500)]
    ] = Field(min_length=1, max_length=10)

class InvoiceConfirmInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    request_id: RequestID
    index: int = Field(ge=1, le=10)
    action: Literal["confirm"] = "confirm"
    corrections: dict[str, Any] = Field(default_factory=dict)


