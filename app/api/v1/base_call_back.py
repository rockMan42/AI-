import asyncio
import logging

from fastapi import APIRouter, HTTPException, Request
from sqlalchemy import select

from app.core.database import create_session
from app.models import User
from app.security.auth import ACTIVE_STATUSES
from app.security.feishu_event import decode_feishu_event
from app.services import holiday as service
from app.services.holiday_cron import HolidayError

router = APIRouter()

log = logging.getLogger(__name__)

@router.post("/feishu/callback/card")
async def card_callback(request: Request):
    data = await decode_feishu_event(request)

    if data.get("type") == "url_verification":
        return {"challenge": data["challenge"]}

    if (data.get("header") or {}).get("event_type") != "card.action.trigger":
        raise HTTPException(400, "不支持的回调类型")

    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value")
    if not isinstance(value, dict):
        raise HTTPException(422, "卡片操作参数无效")

    # 一个回调入口分发不同业务卡片。
    if value.get("module") == "leave":
        from app.api.v1.feishu_leave import handle_leave_card_data

        return await handle_leave_card_data(data)

    if value.get("module") == "requisition":
        from app.api.v1.feishu_requisition import (
            handle_requisition_card_data,
        )

        return await handle_requisition_card_data(data)

    if value.get("module") == "invoice":
        from app.api.v1.invoice import handle_invoice_card_data

        return await handle_invoice_card_data(
            data,
            request.app.state.invoice_service,
        )

    if value.get("module") == "expense":
        from app.api.v1.feishu_expense import handle_expense_card_data

        return await handle_expense_card_data(data)

    if value.get("module") == "lead":
        from app.api.v1.lead import handle_lead_card_data

        return await handle_lead_card_data(data)

    open_id = (event.get("operator") or {}).get("open_id", "")
    operation = value.get("action")

    try:
        async with asyncio.timeout(2):
            if operation in {"confirm_notice", "cancel_notice_draft"}:
                message = await service.confirm_notice_draft(
                    open_id,
                    str(value.get("identifier", "")),
                    cancel=operation == "cancel_notice_draft",
                )
                return {
                    "toast": {"type": "success", "content": message}
                }

            if operation != "confirm_receipt":
                raise HolidayError("不支持的卡片操作", 422)

            notice_id = value.get("notice_id")
            claimed_user_id = value.get("user_id")

            if type(notice_id) is not int or notice_id <= 0:
                raise HolidayError("通知编号无效", 422)

            # 操作者身份来自已验签事件，不相信按钮中的 user_id。
            async with create_session() as db:
                user = await db.scalar(
                    select(User).where(User.feishu_open_id == open_id)
                )
                if user is None or user.status not in ACTIVE_STATUSES:
                    raise HolidayError("用户不存在或已停用", 403)

            if (
                    type(claimed_user_id) is not int
                    or claimed_user_id != user.user_id
            ):
                raise HolidayError("不能代其他员工确认通知", 403)

            notice, user_id = await service.confirm_receipt(
                open_id, notice_id,
            )
            updated_card = await service.confirmed_card(
                notice, user_id,
            )

        result = {
            "toast": {
                "type": "success",
                "content": "已确认收到通知",
            },
        }
        if updated_card is not None:
            result["card"] = {
                "type": "raw",
                "data": updated_card,
            }
        return result

    except HolidayError as exc:
        if exc.status_code == 403:
            raise HTTPException(403, str(exc)) from None
        return {"toast": {"type": "error", "content": str(exc)}}
    except TimeoutError:
        return {
            "toast": {
                "type": "warning",
                "content": "处理结果暂未确认，请再次点击原按钮或查询。",
            }
        }
    except Exception as exc:
        log.error(
            "holiday_callback_failed error_type=%s",
            type(exc).__name__,
        )
        return {
            "toast": {
                "type": "error",
                "content": "服务暂不可用，请重试",
            }
        }


