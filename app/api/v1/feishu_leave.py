import asyncio
import logging
import re

from fastapi import APIRouter, HTTPException, Request
from pydantic import ValidationError
from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.user import User
from app.schemas.leave import ApprovalInput
from app.security.auth import ACTIVE_STATUSES
from app.security.feishu_event import decode_feishu_event
from app.services.attendance.leave_chat import finish_leave_flow
from app.services.attendance.leave_cards import card as leave_card, reject_form_card, request_card
from app.services.attendance.leave_service import LeaveError, LeaveService
from app.services.attendance.feishu_card import text_block


log = logging.getLogger(__name__)
router = APIRouter()


async def callback_actor(open_id: str) -> User:
    allowed = {
        value.strip()
        for value in get_settings().feishu_allowed_users.split(",")
        if value.strip()
    }
    if open_id not in allowed:
        raise HTTPException(403, "用户不在白名单中")

    async with create_session() as db:
        actor = await db.scalar(
            select(User).where(User.feishu_open_id == open_id)
        )
        if actor is None or actor.status not in ACTIVE_STATUSES:
            raise HTTPException(403, "用户不存在或已停用")
        db.expunge(actor)
        return actor

async def handle_leave_card_data(data: dict):
    if data.get("type") == "url_verification":
        return {"challenge": data["challenge"]}

    if data["header"].get("event_type") != "card.action.trigger":
        raise HTTPException(400, "不支持的回调类型")

    event = data.get("event") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    value = (event.get("action") or {}).get("value") or {}

    if not isinstance(value, dict) or value.get("module") != "leave":
        raise HTTPException(400, "卡片操作无效")

    operation = value.get("operation")
    identifier = value.get("identifier", "")
    if not isinstance(identifier, str):
        raise HTTPException(422, "操作编号无效")

    pattern = (
        r"[a-f0-9]{32}"
        if operation in {"confirm", "cancel_draft"}
        else r"LV[0-9A-F]{32}"
    )
    if re.fullmatch(pattern, identifier) is None:
        raise HTTPException(422, "操作编号无效")

    try:
        # 回调内只执行数据库操作，不等待飞书发送，也不调用LLM。
        async with asyncio.timeout(2):
            actor = await callback_actor(open_id)
            service = LeaveService()
            result = None
            updated_card = None

            if operation == "confirm":
                result = await service.submit(open_id, identifier)
                message = f"已提交：{result['request_id']}"
                updated_card = request_card(result)
            elif operation == "cancel_draft":
                result = await service.cancel_draft(open_id, identifier)
                message = "已取消本次填写"
                updated_card = leave_card("请假填写已取消", [text_block(message)], "grey")
            elif operation == "approve":
                result = await service.decide(
                    open_id,
                    identifier,
                    ApprovalInput(action="approve"),
                )
                message = "审批通过"
                updated_card = request_card(result)
            elif operation == "reject":
                fields = (event.get("action") or {}).get("form_value") or {}
                if not isinstance(fields, dict):
                    raise LeaveError("驳回原因格式无效", 422)
                result = await service.decide(
                    open_id, identifier,
                    ApprovalInput(action="reject", reject_reason=str(fields.get("reason") or "")),
                )
                message = "审批已驳回"
                updated_card = request_card(result)
            elif operation == "cancel_request":
                result = await service.cancel(open_id, identifier)
                message = "申请已撤销"
                updated_card = request_card(result)
            elif operation == "reject_hint":
                detail = await service.detail(open_id, identifier)
                if not detail["can_approve"]:
                    raise LeaveError("只有指定审批人可以审批", 403)
                if detail["status"] != "pending":
                    raise LeaveError("申请已处理")
                message = "请填写驳回原因"
                updated_card = reject_form_card("填写请假驳回原因", value)
            else:
                raise LeaveError("不支持的操作", 422)

        if result is not None:
            # 给Redis收尾单独短预算，不改变已提交的数据库事实。
            try:
                async with asyncio.timeout(0.2):
                    await finish_leave_flow(
                        actor.user_id,
                        result.get("flow_key"),
                    )
            except TimeoutError:
                pass

        response = {"toast": {"type": "success", "content": message}}
        if updated_card is not None:
            response["card"] = {"type": "raw", "data": updated_card}
        return response
    except LeaveError as exc:
        # 真正的HTTP403，不能只在JSON正文中放code=403。
        if exc.status_code == 403:
            raise HTTPException(403, str(exc)) from None
        return {"toast": {"type": "error", "content": str(exc)}}
    except ValidationError:
        return {"toast": {"type": "error", "content": "操作参数无效"}}
    except TimeoutError:
        return {
            "toast": {
                "type": "warning",
                "content": "处理结果暂未确认，请查询申请或再次点击原按钮。",
            }
        }
    except HTTPException:
        raise
    except Exception as exc:
        log.error("leave_callback_failed error_type=%s", type(exc).__name__)
        return {"toast": {"type": "error", "content": "服务暂不可用，请重试"}}
