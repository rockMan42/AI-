import asyncio
import logging

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import ValidationError
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
from typing import Annotated

from app.core.database import create_session, get_session
from app.models import User
from app.schemas.lead import FollowUpInput, LeadQuery
from app.security.auth import get_current_user
from app.security.lead import LeadError
from app.services.lead import LeadService
from app.services.lead_cards import (
    detail_card, follow_up_form, follow_up_saved_card, query_card,
)
from app.services.card_form import parse_form_values


router = APIRouter(prefix="/leads")
service = LeadService()
log = logging.getLogger(__name__)

DB = Annotated[AsyncSession, Depends(get_session)]
Actor = Annotated[User, Depends(get_current_user)]
QueryParams = Annotated[LeadQuery, Query()]


async def invoke(operation):
    try:
        return {"code": 200, "data": await operation}
    except LeadError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None


@router.get("")
async def query_leads(params: QueryParams, actor: Actor, db: DB):
    return await invoke(service.query(db, actor, params))


# 放在 /{lead_id} 前面，避免 priority 被当成线索编号。
@router.get("/priority")
async def query_priority(params: QueryParams, actor: Actor, db: DB):
    params = params.model_copy(update={"sort_by": "priority_score"})
    return await invoke(service.query(db, actor, params))


@router.get("/{lead_id}")
async def get_detail(lead_id: int, actor: Actor, db: DB):
    return await invoke(service.detail(db, actor, lead_id))


@router.post("/{lead_id}/follow-up")
async def record_follow_up(
    lead_id: int,
    body: FollowUpInput,
    actor: Actor,
    db: DB,
):
    return await invoke(service.follow_up(db, actor, lead_id, body))


async def handle_lead_card_data(data: dict) -> dict:
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    operation = value.get("operation")

    try:
        async with asyncio.timeout(2):
            async with create_session() as db:
                actor = await db.scalar(
                    select(User).where(User.feishu_open_id == open_id)
                )
                if actor is None:
                    raise LeadError("用户不存在", 403)
                actor = await service.actor(db, actor.user_id)

                if operation == "query":
                    params = LeadQuery.model_validate(value.get("params") or {})
                    result = await service.query(db, actor, params)
                    result_card = query_card(
                        result, params.model_dump(mode="json"),
                    )
                elif operation in {"detail", "follow_up", "follow_up_save"}:
                    lead_id = value.get("lead_id")
                    if type(lead_id) is not int or lead_id <= 0:
                        raise LeadError("线索卡片已失效，请重新查询", 422)

                    if operation == "detail":
                        history_page = value.get("history_page", 1)
                        if type(history_page) is not int or history_page < 1:
                            raise LeadError("记录页码无效", 422)
                        result = await service.detail(db, actor, lead_id)
                        result_card = detail_card(result, history_page)
                    elif operation == "follow_up":
                        lead = await service.load(db, actor, lead_id)
                        if lead.assigned_to != actor.user_id:
                            raise LeadError("只能跟进自己负责的线索", 403)
                        result_card = follow_up_form(lead_id, lead.company_name)
                    else:
                        allowed = {"follow_up_type", "content", "outcome",
                                   "next_action", "next_follow_up"}
                        try:
                            fields = parse_form_values(
                                (event.get("action") or {}).get("form_value") or {}, allowed,
                            )
                        except ValueError:
                            raise LeadError("跟进卡片已失效，请重新打开", 409) from None
                        body = FollowUpInput.model_validate({
                            name: field or None
                            for name, field in fields.items()
                        })
                        await service.follow_up(db, actor, lead_id, body)
                        lead = await service.detail(db, actor, lead_id)
                        result_card = follow_up_saved_card(
                            lead["company_name"], body.content,
                        )
                else:
                    raise LeadError("不支持的线索操作", 422)

        return {
            "toast": {"type": "success", "content": "操作成功"},
            "card": {"type": "raw", "data": result_card},
        }
    except (LeadError, ValidationError, ValueError) as exc:
        if isinstance(exc, LeadError) and exc.status_code == 403:
            raise HTTPException(403, str(exc)) from None
        message = (
            "操作参数格式不正确"
            if isinstance(exc, ValidationError) else str(exc)
        )
        return {"toast": {"type": "error", "content": message}}
    except TimeoutError:
        return {
            "toast": {
                "type": "warning",
                "content": "处理超时，请稍后重试",
            },
        }
    except Exception:
        log.exception("lead_card_failed")
        return {"toast": {"type": "error", "content":
                "线索暂时无法处理，请稍后重试"}}
