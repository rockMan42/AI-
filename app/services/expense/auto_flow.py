import logging
from datetime import date, timedelta
from uuid import NAMESPACE_URL, uuid5

from pydantic import ValidationError
from sqlalchemy import select

from app.core.database import create_session
from app.models import Invoice, User
from app.models.expense_flow import BusinessTrip
from app.schemas.expense import ExpenseSupplement
from app.services.conversation_engine.feishu import send_feishu_card
from app.services.expense.cards import card, safe_text


log = logging.getLogger(__name__)


def infer_expense_type(ocr: dict) -> str | None:
    text = " ".join(
        str(ocr.get(name) or "")
        for name in ("invoice_type", "items_description", "seller_name")
    )
    mappings = (
        (("住宿", "酒店", "宾馆", "客房"), "hotel"),
        (("餐饮", "餐费", "饭店", "餐厅"), "meal"),
        (("航空", "机票", "客票", "航班"), "flight"),
        (("铁路", "火车票", "动车", "高铁"), "train"),
        (("出租车", "网约车", "打车"), "taxi"),
    )
    matches = {
        expense_type
        for keywords, expense_type in mappings
        if any(keyword in text for keyword in keywords)
    }
    return next(iter(matches)) if len(matches) == 1 else None


def inferred_supplement(ocr: dict) -> ExpenseSupplement:
    expense_type = infer_expense_type(ocr)
    if expense_type is None:
        raise ValueError("无法自动判断费用类型")

    return ExpenseSupplement(
        expense_type=expense_type,
        city=ocr.get("expense_city"),
        check_in=ocr.get("service_start_date"),
        check_out=ocr.get("service_end_date"),
        seat=ocr.get("transport_seat"),
    )


def supplement_needed_card(request_id: str, missing: list[dict]) -> dict:
    return card(
        "还需要补充报销信息",
        [
            {"tag": "markdown", "content": "请补齐以下发票的费用信息，完成后会继续生成报销单。"},
            *[
                {"tag": "action", "actions": [{
                    "tag": "button",
                    "text": {"tag": "plain_text", "content": f"补充第 {item['index']} 张发票"},
                    "value": {
                        "module": "expense", "operation": "supplement_open",
                        "request_id": request_id, "index": item["index"],
                    },
                }]}
                for item in missing
            ],
        ],
        "orange",
    )


def supplement_form_card(request_id: str, item: dict) -> dict:
    current = item.get("_expense") or {}
    expense_type = current.get("expense_type") or infer_expense_type(item)
    fields = [
        {"tag": "select_static", "name": "expense_type",
         "placeholder": {"tag": "plain_text", "content": "选择费用类型"},
         **({"initial_option": expense_type} if expense_type else {}),
         "options": [
             {"text": {"tag": "plain_text", "content": label}, "value": value}
             for value, label in (("hotel", "住宿"), ("meal", "餐饮"),
                                  ("train", "火车"), ("flight", "飞机"),
                                  ("taxi", "出租车"), ("other", "其他"))
         ]},
    ]
    for name, label, source in (
        ("city", "费用发生城市", "expense_city"),
        ("check_in", "入住日期（YYYY-MM-DD）", "service_start_date"),
        ("check_out", "退房日期（YYYY-MM-DD）", "service_end_date"),
        ("seat", "座席或舱位", "transport_seat"),
    ):
        fields.append({
            "tag": "input", "name": name,
            "label": {"tag": "plain_text", "content": label},
            "default_value": str(current.get(name) or item.get(source) or ""),
            "required": False,
        })
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": f"补充第 {item['index']} 张发票"}},
        "body": {"elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content":
                "住宿需填写城市和入住、退房日期；火车或飞机需填写座席或舱位。"}},
            {"tag": "form", "name": "expense_supplement", "elements": [
                *fields,
                {"tag": "button", "name": "save_supplement", "type": "primary",
                 "form_action_type": "submit",
                 "text": {"tag": "plain_text", "content": "保存并继续"},
                 "behaviors": [{"type": "callback", "value": {
                     "module": "expense", "operation": "supplement_save",
                     "request_id": request_id, "index": item["index"],
                 }}]},
            ]},
        ]},
    }


def trip_selection_card(
    request_id: str,
    invoice_ids: list[int],
    trips: list[BusinessTrip],
) -> dict:
    elements = [{
        "tag": "markdown",
        "content": (
            "发票信息已准备完成，请选择关联的出差申请。\n"
            f"本次共 {len(invoice_ids)} 张发票。"
        ),
    }]

    actions = []
    for trip in trips[:4]:
        actions.append({
            "tag": "button",
            "type": "primary" if len(trips) == 1 else "default",
            "text": {
                "tag": "plain_text",
                "content": (
                    f"{trip.destination} "
                    f"{trip.start_date:%m-%d}至{trip.end_date:%m-%d}"
                )[:30],
            },
            "value": {
                "module": "expense",
                "operation": "prepare",
                "request_id": request_id,
                "invoice_ids": invoice_ids,
                "trip_id": trip.id,
            },
        })

    actions.append({
        "tag": "button",
        "type": "default",
        "text": {"tag": "plain_text", "content": "非差旅报销"},
        "value": {
            "module": "expense",
            "operation": "prepare",
            "request_id": request_id,
            "invoice_ids": invoice_ids,
            "trip_id": None,
        },
    })
    elements.append({"tag": "action", "actions": actions})
    return card("选择出差申请", elements)


async def build_next_expense_card(user_id: int, batch: dict) -> dict | None:
    """根据已确认发票生成下一步卡片；保留员工手工补充的费用信息。"""
    invoice_ids = [
        item["invoice_id"]
        for item in batch.get("results", [])
        if item.get("invoice_id")
    ]
    if not invoice_ids:
        return None

    missing = []
    invoice_dates = []

    async with create_session() as db, db.begin():
        user = await db.get(User, user_id)
        rows = list((await db.scalars(
            select(Invoice)
            .where(Invoice.id.in_(invoice_ids))
            .order_by(Invoice.id)
            .with_for_update()
        )).all())
        if user is None or len(rows) != len(invoice_ids):
            return None

        index_by_id = {item["invoice_id"]: item["index"] for item in batch["results"]}
        for row in rows:
            payload = dict(row.ocr_result_json or {})
            try:
                issued = date.fromisoformat(str(payload.get("invoice_date")))
                invoice_dates.append(issued)
                if payload.get("_expense"):
                    ExpenseSupplement.model_validate(payload["_expense"])
                else:
                    supplement = inferred_supplement(payload)
                    payload["_expense"] = supplement.model_dump(mode="json")
                    row.ocr_result_json = payload
            except (ValueError, ValidationError):
                missing.append({"index": index_by_id[row.id]})

    if missing:
        return supplement_needed_card(batch["request_id"], missing)
    else:
        lower = min(invoice_dates) - timedelta(days=1)
        upper = max(invoice_dates) + timedelta(days=1)
        async with create_session() as db:
            trips = list((await db.scalars(
                select(BusinessTrip)
                .where(
                    BusinessTrip.user_id == user_id,
                    BusinessTrip.status.in_(["approved", "completed"]),
                    BusinessTrip.start_date <= upper,
                    BusinessTrip.end_date >= lower,
                )
                .order_by(BusinessTrip.start_date.desc())
                .limit(4)
            )).all())
        return trip_selection_card(
            batch["request_id"], invoice_ids, trips,
        )


async def start_auto_expense_flow(user_id: int, batch: dict) -> None:
    """最后一张 OCR 确认后，自动补全可识别信息并发送下一步卡片。"""
    next_card = await build_next_expense_card(user_id, batch)
    if next_card is None:
        return

    async with create_session() as db:
        user = await db.get(User, user_id)
        if user is None:
            return
        open_id = user.feishu_open_id

    await send_feishu_card(
        open_id,
        next_card,
        uuid5(
            NAMESPACE_URL,
            f"expense-next:{batch['request_id']}",
        ).hex,
    )
