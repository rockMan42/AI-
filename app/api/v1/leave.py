import logging
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, HTTPException, Query

from app.models.user import User
from app.schemas.leave import ApprovalInput, DraftAction, LeaveInput
from app.security.auth import get_current_user
from app.services.attendance.leave_service import LeaveError, LeaveService


log = logging.getLogger(__name__)
router = APIRouter(prefix="/attendance/leave-requests")
Actor = Annotated[User, Depends(get_current_user)]


async def invoke(operation):
    try:
        return await operation
    except LeaveError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from None
    except Exception as exc:
        log.error("leave_api_failed error_type=%s", type(exc).__name__)
        raise HTTPException(503, "请假服务暂不可用") from None


@router.post("/preview")
async def preview(body: LeaveInput, actor: Actor):
    return await invoke(
        LeaveService().prepare(actor.feishu_open_id, body)
    )


@router.post("")
async def submit(body: DraftAction, actor: Actor):
    return await invoke(
        LeaveService().submit(actor.feishu_open_id, body.draft_id)
    )


@router.get("")
async def list_requests(
    actor: Actor,
    scope: Literal["mine", "inbox"] = "mine",
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    return await invoke(
        LeaveService().list_requests(
            actor.feishu_open_id,
            scope=scope,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/{request_id}")
async def detail(request_id: str, actor: Actor):
    return await invoke(
        LeaveService().detail(actor.feishu_open_id, request_id)
    )


@router.put("/{request_id}/approve")
async def approve(request_id: str, body: ApprovalInput, actor: Actor):
    return await invoke(
        LeaveService().decide(actor.feishu_open_id, request_id, body)
    )


@router.put("/{request_id}/cancel")
async def cancel(request_id: str, actor: Actor):
    return await invoke(
        LeaveService().cancel(actor.feishu_open_id, request_id)
    )