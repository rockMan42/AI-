import asyncio

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
from app.services.conversation_engine.conversation_manager import ConversationManager
from app.services.conversation_engine.register_skill import get_skill_register
from app.services.conversation_engine.session_store import SessionStore
from app.services.conversation_engine.slot_collector import SlotCollector
from app.services.expense.cards import card
from app.services.lead import LeadService
from app.services.lead_cards import detail_card, markdown, query_card


router = APIRouter(prefix="/leads")
service = LeadService()

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
                elif operation in {"detail", "follow_up"}:
                    lead_id = value.get("lead_id")
                    if type(lead_id) is not int or lead_id <= 0:
                        raise LeadError("线索编号无效", 422)

                    if operation == "detail":
                        history_page = value.get("history_page", 1)
                        if type(history_page) is not int or history_page < 1:
                            raise LeadError("记录页码无效", 422)
                        result = await service.detail(db, actor, lead_id)
                        result_card = detail_card(result, history_page)
                    else:
                        lead = await service.load(db, actor, lead_id)
                        if lead.assigned_to != actor.user_id:
                            raise LeadError("只能跟进自己负责的线索", 403)

                        skill = get_skill_register().get_skill("lead_follow_up")
                        if skill is None:
                            raise LeadError("跟进技能尚未注册", 503)

                        store = SessionStore()
                        session = await ConversationManager(
                            store
                        ).get_or_create_session(
                            actor.user_id, skill.name, skill,
                        )

                        # 点击另一条线索时，不携带上一条未完成的跟进内容。
                        for slot in session.slots.values():
                            slot.value = None
                            slot.filled = False
                            slot.error = None
                        session.pending_slot = None

                        result = await SlotCollector(store).collect(
                            session, skill, {"lead_id": lead_id},
                        )
                        result_card = card(
                            f"记录跟进 · {lead.company_name}",
                            [markdown(result["message"])],
                        )
                else:
                    raise LeadError("不支持的线索操作", 422)

        return {
            "toast": {"type": "success", "content": "操作成功"},
            "card": {"type": "raw", "data": result_card},
        }
    except (LeadError, ValidationError, ValueError) as exc:
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