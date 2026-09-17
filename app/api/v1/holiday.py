
import asyncio
import logging
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from pydantic import ValidationError
from datetime import datetime
from sqlalchemy import select

from app.core.database import create_session
from app.models.user import User
from app.schemas.holiday import (
    CronInput,
    NoticeInput,
    NoticeUpdate,
    TextReceiptInput,
)
from app.security.auth import ACTIVE_STATUSES, get_current_user
from app.security.feishu_event import decode_feishu_event
from app.services import holiday as service
from app.services.holiday_cron import (
    HolidayError,
    execute_task,
    notice_lease,
    now_ms,
    push_task_id,
    read_task,
    register_task,
)

log = logging.getLogger(__name__)
router = APIRouter()
Actor = Annotated[User, Depends(get_current_user)]


async def invoke(operation):
    try:
        return {"code": 0, "data": await operation}
    except HolidayError as exc:
        raise HTTPException(exc.status_code, str(exc)) from None
    except ValidationError:
        raise HTTPException(422, "参数无效") from None
    except Exception as exc:
        log.exception(
            "holiday_api_failed error_type=%s",
            type(exc).__name__,
        )
        raise HTTPException(503, "节假日通知服务暂不可用") from None

async def create_and_detail(open_id, body):
    notice_id = await service.create_notice(open_id, body)
    return await service.detail(open_id, notice_id)


@router.post("/holiday-notices")
async def create(body: NoticeInput, user: Actor):
    return await invoke(
        create_and_detail(user.feishu_open_id, body)
    )

async def update_and_detail(open_id, notice_id, body):
    await service.update_notice(open_id, notice_id, body)
    return await service.detail(open_id, notice_id)

@router.put("/holiday-notices/{notice_id}")
async def update_notice(
    notice_id: int,
    body: NoticeUpdate,
    user: Actor,
):
    return await invoke(
        update_and_detail(user.feishu_open_id, notice_id, body)
    )

@router.get("/holiday-notices")
async def list_notices(
    user: Actor,
    keyword: str | None = Query(default=None, max_length=200),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    return await invoke(
        service.list_notices(
            user.feishu_open_id,
            keyword=keyword,
            offset=offset,
            limit=limit,
        )
    )


@router.get("/holiday-notices/{notice_id}")
async def notice_detail(notice_id: int, user: Actor):
    return await invoke(
        service.detail(user.feishu_open_id, notice_id)
    )

async def push_now(open_id: str, notice_id: int):
    await service.require_management(open_id)
    task = await read_task(push_task_id(notice_id))
    if not task:
        raise HolidayError("通知尚未注册推送任务", 409)
    if task["status"] == "FAILED":
        raise HolidayError("推送已失败，请先核对任务与投递状态")

    # 手动调用执行器，不等待Cron到期。
    return await execute_task(
        push_task_id(notice_id),
        force=True,
    )

@router.post("/holiday-notices/{notice_id}/push")
async def manual_push(notice_id: int, user: Actor):
    return await invoke(
        push_now(user.feishu_open_id, notice_id)
    )

async def register_push(open_id: str, body: CronInput):
    await service.require_management(open_id)
    notice_id = body.payload.notice_id

    async with notice_lease(notice_id):
        notice = await service.load_notice(notice_id)
        existing = await read_task(push_task_id(notice_id))

        if notice.status != "draft":
            raise HolidayError("仅草稿通知允许重新设置推送任务")
        if not existing:
            raise HolidayError(
                "缺少完整安排，请通过更新通知接口重新保存",
            )

        payload = existing["payload"]
        deadline = int(
            service.datetime.fromisoformat(
                payload["body"]["deadline"]
            ).timestamp() * 1000
        )
        execute_at = max(
            now_ms(),
            int(body.execute_at.timestamp() * 1000),
        )
        if execute_at >= deadline:
            raise HolidayError("推送时间必须早于确认截止时间", 422)

        payload.update(body.payload.model_dump())
        await register_task(
            push_task_id(notice_id),
            body.task_type,
            payload,
            execute_at,
            max_retries=body.max_retries,
            replace=True,
        )

    return {
        "task_id": push_task_id(notice_id),
        "execute_at": execute_at,
        "status": "PENDING",
    }

@router.post("/cron-tasks")
async def create_cron(body: CronInput, user: Actor):
    return await invoke(
        register_push(user.feishu_open_id, body)
    )


async def confirm_text(open_id: str, body: TextReceiptInput):
    if body.text not in service.RECEIPT_WORDS:
        raise HolidayError("请使用收到、确认、好的或知道了", 422)

    notice, _ = await service.confirm_receipt(
        open_id, body.notice_id,
    )
    return {
        "notice_id": notice.id if notice else None,
        "message": (
            f"✅ 已确认收到【{notice.holiday_name}】放假通知"
            if notice else "您当前没有待确认的通知"
        ),
    }

@router.post("/notice-receipts/confirm-by-text")
async def confirm_by_text(body: TextReceiptInput, user: Actor):
    return await invoke(
        confirm_text(user.feishu_open_id, body)
    )

@router.get("/holiday-notices/{notice_id}/receipt-stats")
async def receipt_stats(notice_id: int, user: Actor):
    return await invoke(
        service.receipt_stats(user.feishu_open_id, notice_id)
    )

async def unconfirmed(open_id: str, notice_id: int):
    stats = await service.receipt_stats(open_id, notice_id)
    return {
        "notice_id": notice_id,
        "items": stats["unconfirmed_users"],
    }

@router.get("/holiday-notices/{notice_id}/unconfirmed")
async def unconfirmed_users(notice_id: int, user: Actor):
    return await invoke(
        unconfirmed(user.feishu_open_id, notice_id)
    )

@router.post("/holiday-notices/{notice_id}/remind")
async def remind(notice_id: int, user: Actor):
    return await invoke(
        service.request_reminder(user.feishu_open_id, notice_id)
    )

@router.post("/holiday-notices/{notice_id}/report")
async def report(notice_id: int, user: Actor):
    return await invoke(
        service.request_report(user.feishu_open_id, notice_id)
    )