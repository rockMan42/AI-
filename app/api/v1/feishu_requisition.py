import asyncio
import logging
import re

from fastapi import HTTPException
from sqlalchemy import select

from app.core.database import create_session
from app.models.user import User
from app.security.auth import ACTIVE_STATUSES
from app.services.requisition.cards import requisition_result_card
from app.services.conversation_engine.session_store import (
    SessionStore,
)
from app.services.requisition.requisition_service import (
    RequisitionError,
    RequisitionService,
)


logger = logging.getLogger(__name__)


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

    if operation not in {"confirm", "cancel"}:
        raise HTTPException(422, "不支持的申领操作")
    if (
        not isinstance(draft_id, str)
        or re.fullmatch(r"[a-f0-9]{32}", draft_id) is None
    ):
        raise HTTPException(422, "申领草稿编号无效")

    try:
        async with asyncio.timeout(15):
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

                if operation == "cancel":
                    await service.cancel_draft(
                        user.user_id,
                        draft_id,
                    )
                    message = "已取消本次物资申领"
                else:
                    result = await service.submit_draft(
                        db,
                        user,
                        draft_id,
                    )
                    message = (
                        "申领单已提交成功："
                        f"REQ-{result['requisition_id']}"
                    )

                session = await SessionStore().load(
                    str(user.user_id)
                )
                if (
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
                "data": requisition_result_card(
                    message,
                    cancelled=operation == "cancel",
                ),
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
                "content": "处理超时，请再次点击或发送“重试”",
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
                "content": "申领处理失败，信息已保留，请稍后发送“重试”",
            }
        }
