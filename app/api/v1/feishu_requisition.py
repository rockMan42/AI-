import asyncio
import logging
import re

from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import create_session
from app.models.user import User
from app.models.requisition import Requisition
from app.hermes.requisition_mcp_client import call_requisition_tool, RequisitionMCPError
from app.config.settings import get_settings
from app.security.auth import ACTIVE_STATUSES
from app.services.requisition.cards import (
    requisition_confirmation_card, requisition_edit_card,
    requisition_result_card,
)
from app.services.requisition.approval_poll_service import build_status_card
from app.services.conversation_engine.session_store import (
    SessionStore,
)
from app.services.requisition.requisition_service import (
    RequisitionError,
    RequisitionService,
)
from app.services.card_form import parse_form_values


logger = logging.getLogger(__name__)


def callback_timeout() -> float:
    return get_settings().requisition_card_timeout_seconds


async def handle_requisition_card_data(
    data: dict,
) -> dict:
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (
        (event.get("operator") or {}).get("open_id")
        or ""
    )

    operation = value.get("operation")
    draft_id = value.get("draft_id")

    if operation == "status":
        requisition_id = value.get("requisition_id")
        if type(requisition_id) is not int or requisition_id <= 0:
            return {"toast": {"type": "error", "content":
                    "进度卡片已失效，请重新查询"}}
        try:
            async with asyncio.timeout(callback_timeout()):
                async with create_session() as db:
                    user = await db.scalar(select(User).where(
                        User.feishu_open_id == open_id,
                        User.status.in_(ACTIVE_STATUSES),
                    ))
                    if user is None:
                        raise HTTPException(403, "用户不存在或已停用")
                    row = await db.get(Requisition, requisition_id)
                    if row is None or row.user_id != user.user_id:
                        raise HTTPException(403, "无权查看这笔申领")
                    status_data = await call_requisition_tool(
                        "query_requisition",
                        {"requisition_id": row.id},
                        user_id=user.user_id,
                    )
                    result_card = build_status_card(row, status_data)
            return {"toast": {"type": "success", "content": "进度已更新"},
                    "card": {"type": "raw", "data": result_card}}
        except HTTPException:
            raise
        except (TimeoutError, RequisitionMCPError):
            return {"toast": {"type": "warning", "content":
                    "审批进度暂时无法更新，请稍后重试"}}
        except Exception:
            logger.exception("requisition_status_card_failed")
            return {"toast": {"type": "error", "content":
                    "审批进度暂时无法查看，请稍后重试"}}

    if operation not in {"confirm", "cancel", "edit", "save"}:
        raise HTTPException(422, "不支持的申领操作")
    if (
        not isinstance(draft_id, str)
        or re.fullmatch(r"[a-f0-9]{32}", draft_id) is None
    ):
        raise HTTPException(422, "申领草稿编号无效")

    try:
        async with asyncio.timeout(callback_timeout()):
            async with create_session() as db:
                user = await db.scalar(
                    select(User).where(
                        User.feishu_open_id == open_id
                    )
                )
                if (
                    user is None
                    or user.status not in ACTIVE_STATUSES
                ):
                    raise RequisitionError(
                        "用户不存在或已停用",
                        403,
                    )

                service = RequisitionService()

                if operation in {"edit", "save"}:
                    draft = await service.get_draft(draft_id)
                    if draft is None:
                        raise RequisitionError("申领草稿已过期，请重新填写")
                    if draft["user_id"] != user.user_id:
                        raise RequisitionError("只能修改本人的申领草稿", 403)
                    if operation == "edit":
                        updated_card = requisition_edit_card(draft)
                        message = "请修改后预览"
                    else:
                        allowed = {"item_category", "item_name", "specification",
                                   "quantity", "reason", "purpose",
                                   "expected_return_date"}
                        try:
                            fields = parse_form_values(
                                (event.get("action") or {}).get("form_value") or {},
                                allowed,
                            )
                        except ValueError:
                            raise RequisitionError("修改卡片已失效，请重新打开") from None
                        slots = {name: fields.get(name, draft.get(name)) for name in allowed}
                        if not str(slots.get("quantity") or "").isdigit():
                            raise RequisitionError("数量必须是正整数", 422)
                        slots["quantity"] = int(slots["quantity"])
                        new_draft = await service.prepare(
                            db, user, slots, replace_draft_id=draft_id,
                        )
                        updated_card = requisition_confirmation_card(new_draft)
                        message = "已更新，请核对后确认提交"
                elif operation == "cancel":
                    await service.cancel_draft(
                        user.user_id,
                        draft_id,
                    )
                    message = "已取消本次物资申领"
                    updated_card = requisition_result_card(message, cancelled=True)
                else:
                    result = await service.submit_draft(
                        db,
                        user,
                        draft_id,
                    )
                    message = "申领已提交，审批进度将通过飞书通知您"
                    updated_card = requisition_result_card(message)

                session = await SessionStore().load(
                    str(user.user_id)
                )
                if (
                    operation in {"confirm", "cancel"}
                    and
                    session is not None
                    and session.intent_code
                    == "requisition_apply"
                ):
                    session.status = "completed"
                    session.state = "completed"
                    session.workflow_state = "completed"
                    session.pending_slot = None
                    await SessionStore().save(session)

        return {
            "toast": {
                "type": "success",
                "content": message,
            },
            "card": {
                "type": "raw",
                "data": updated_card,
            },
        }
    except RequisitionError as exc:
        if exc.status_code == 403:
            raise HTTPException(403, str(exc)) from None
        return {
            "toast": {
                "type": "error",
                "content": str(exc),
            }
        }
    except TimeoutError:
        return {
            "toast": {
                "type": "warning",
                "content": "处理超时，请稍后再次点击“确认提交”",
            }
        }
    except Exception:
        logger.exception(
            "requisition_card_callback_failed operation=%s draft_id=%s",
            operation,
            draft_id,
        )
        return {
            "toast": {
                "type": "error",
                "content": "申领处理失败，信息已保留，请稍后再次点击“确认提交”",
            }
        }
