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


def supplement_needed_card(invoice_ids: list[int], messages: list[str]) -> dict:
    return card(
        "还需要补充报销信息",
        [{
            "tag": "markdown",
            "content": (
                "OCR 已全部确认，但以下信息无法自动确定：\n"
                + "\n".join(safe_text(message) for message in messages)
                + "\n\n请使用原有“补充报销 发票ID”方式补充；"
                  "补充完成后可继续使用“生成报销”作为备用入口。"
            ),
        }],
        "orange",
    )


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


async def start_auto_expense_flow(user_id: int, batch: dict) -> None:
    """最后一张 OCR 确认后，自动补全可识别信息并发送下一步卡片。"""
    invoice_ids = [
        item["invoice_id"]
        for item in batch.get("results", [])
        if item.get("invoice_id")
    ]
    if not invoice_ids:
        return

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
            return

        for row in rows:
            payload = dict(row.ocr_result_json or {})
            try:
                issued = date.fromisoformat(str(payload.get("invoice_date")))
                invoice_dates.append(issued)
                supplement = inferred_supplement(payload)
                payload["_expense"] = supplement.model_dump(mode="json")
                row.ocr_result_json = payload
            except (ValueError, ValidationError) as exc:
                missing.append(f"发票 {row.id}：{exc}")

        open_id = user.feishu_open_id

    if missing:
        next_card = supplement_needed_card(invoice_ids, missing)
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
        next_card = trip_selection_card(
            batch["request_id"], invoice_ids, trips,
        )

    await send_feishu_card(
        open_id,
        next_card,
        uuid5(
            NAMESPACE_URL,
            f"expense-next:{batch['request_id']}",
        ).hex,
    )
