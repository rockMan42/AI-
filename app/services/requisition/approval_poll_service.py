import json
import logging
from datetime import datetime
from platform import system
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core import redis_client as redis_module
from app.core.database import create_session
from app.hermes.requisition_mcp_client import (
    call_requisition_tool,
)
from app.models.requisition import (
    Requisition,
    RequisitionApproval,
)
from app.models.user import User
from app.services.attendance.leave_calendar import (
    CalendarUnavailable,
    load_calendar,
)
from app.services.conversation_engine.feishu import (
    send_feishu_card,
)
from app.services.holiday_cron import (
    finish_task,
    now_ms,
    read_task,
    register_task,
    task_key,
)
from app.services.requisition.requisition_service import (
    STATUS_TEXT,
    TERMINAL_STATUSES,
)
from app.utils.time import as_shanghai, utc_now


log = logging.getLogger(__name__)
TASK_TYPE = "APPROVAL_POLL"
MAX_POLL_DAYS = 30


def poll_task_id(requisition_id: int) -> str:
    return f"{TASK_TYPE}:{requisition_id}"


def poll_interval_ms(poll_count: int) -> int:
    if poll_count <= 5:
        return 5 * 60 * 1000
    if poll_count <= 20:
        return 15 * 60 * 1000
    if poll_count <= 50:
        return 30 * 60 * 1000
    return 60 * 60 * 1000


async def register_approval_poll(
    requisition_id: int,
    applicant_user_id: int,
    last_status: str = "pending",
) -> str:
    task_id = poll_task_id(requisition_id)

    await register_task(
        task_id,
        TASK_TYPE,
        {
            "requisition_id": requisition_id,
            "applicant_user_id": applicant_user_id,
            "poll_count": 0,
            "last_status": last_status,
            "max_poll_days": MAX_POLL_DAYS,
        },
        now_ms() + 5 * 60 * 1000,
        max_retries=1,
        replace=False,
    )

    await redis_module.redis_client.expire(
        task_key(task_id),
        MAX_POLL_DAYS * 24 * 60 * 60,
    )
    return task_id


async def _reschedule(
    task: dict,
    payload: dict,
    delay_ms: int,
) -> None:
    execute_at = now_ms() + delay_ms
    client = redis_module.redis_client

    async with client.pipeline(transaction=True) as pipe:
        pipe.hset(
            task_key(task["task_id"]),
            mapping={
                "status": "PENDING",
                "payload": json.dumps(
                    payload,
                    ensure_ascii=False,
                ),
                "execute_at": str(execute_at),
            },
        )
        pipe.zadd(
            "dep:cron:queue",
            {task["task_id"]: execute_at},
        )
        await pipe.execute()


def _parse_oa_time(value: str | None) -> datetime | None:
    if not value:
        return None
    parsed = datetime.fromisoformat(
        value.replace("Z", "+00:00")
    )
    return parsed.replace(tzinfo=None)


async def _sync_local_status(
    db: AsyncSession,
    requisition: Requisition,
    status_data: dict,
) -> None:
    requisition.status = status_data["status"]

    current = status_data.get("current_approver")
    current_id = (
        current.get("approver_id")
        if isinstance(current, dict)
        else status_data.get("current_approver_id")
    )
    if type(current_id) is int:
        user = await db.get(User, current_id)
        requisition.approver_id = (
            user.user_id if user else None
        )
    elif status_data["status"] in TERMINAL_STATUSES:
        requisition.approver_id = None

    existing = (
        await db.execute(
            select(
                RequisitionApproval.approver_id,
                RequisitionApproval.action,
                RequisitionApproval.created_at,
            ).where(
                RequisitionApproval.requisition_id
                == requisition.id
            )
        )
    ).all()
    existing_keys = {
        (row.approver_id, row.action, row.created_at)
        for row in existing
    }

    for node in status_data.get("approval_chain") or []:
        action = {
            "approved": "approve",
            "approve": "approve",
            "rejected": "reject",
            "reject": "reject",
        }.get(node.get("action"))

        approver_id = node.get("approver_id")
        action_time = _parse_oa_time(
            node.get("action_time")
        )

        if (
            action is None
            or type(approver_id) is not int
            or action_time is None
        ):
            continue

        if (
            approver_id,
            action,
            action_time,
        ) in existing_keys:
            continue

        if await db.get(User, approver_id) is None:
            log.warning(
                "requisition_approver_not_found "
                "requisition_id=%s approver_id=%s",
                requisition.id,
                approver_id,
            )
            continue

        db.add(
            RequisitionApproval(
                requisition_id=requisition.id,
                approver_id=approver_id,
                action=action,
                comment=node.get("comment"),
                created_at=action_time,
            )
        )

    await db.commit()


def build_status_card(
    requisition: Requisition,
    status_data: dict,
) -> dict:
    status = status_data["status"]
    style = {
        "pending": ("orange", "⏳"),
        "approving": ("blue", "🔄"),
        "approved": ("green", "✅"),
        "rejected": ("red", "❌"),
        "fulfilled": ("green", "📦"),
    }
    template, icon = style.get(status, ("blue", "📋"))

    lines = [
        f"**{icon} {STATUS_TEXT[status]}**",
        f"申领单号：REQ-{requisition.id}",
        f"物资：{requisition.item_name} × "
        f"{requisition.quantity}",
    ]

    chain = []
    for node in status_data.get("approval_chain") or []:
        action = node.get("action")
        mark = (
            "✅"
            if action in {"approve", "approved"}
            else "❌"
            if action in {"reject", "rejected"}
            else "⏳"
        )
        chain.append(
            f"{node.get('approver_name', '审批人')}{mark}"
        )

    current = status_data.get("current_approver")
    if isinstance(current, dict):
        current_name = current.get("approver_name")
    else:
        current_name = current

    if current_name and status not in TERMINAL_STATUSES:
        chain.append(f"{current_name}🔄")

    if chain:
        lines.append(f"审批链路：{' → '.join(chain)}")

    if (
        status == "rejected"
        and status_data.get("reject_reason")
    ):
        lines.append(
            f"驳回原因：{status_data['reject_reason']}"
        )

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": template,
            "title": {
                "tag": "plain_text",
                "content": "📦 物资申领进度",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "\n".join(lines),
            }
        ],
    }


async def _notify_status_change(
    requisition: Requisition,
    applicant: User,
    status_data: dict,
) -> None:
    event_version = (
        status_data.get("updated_at")
        or status_data["status"]
    )
    message_uuid = uuid5(
        NAMESPACE_URL,
        f"requisition:{requisition.id}:"
        f"{status_data['status']}:{event_version}",
    ).hex

    await send_feishu_card(
        applicant.feishu_open_id,
        build_status_card(requisition, status_data),
        message_uuid,
    )


async def _check_overtime_warning(
    db: AsyncSession,
    requisition: Requisition,
    applicant: User,
    status_data: dict,
) -> None:
    if requisition.status not in {"pending", "approving"}:
        return

    client = redis_module.redis_client
    warned_key = (
        f"dep:approval:warned:{requisition.id}"
    )
    if await client.exists(warned_key):
        return

    start = as_shanghai(requisition.created_at).date()
    today = as_shanghai(utc_now()).date()

    try:
        calendar = await load_calendar(db, start, today)
    except CalendarUnavailable:
        log.warning(
            "requisition_calendar_incomplete "
            "requisition_id=%s",
            requisition.id,
        )
        return

    elapsed = sum(
        1
        for day, is_workday in calendar.items()
        if day > start and is_workday
    )
    if elapsed < 3:
        return

    card = {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "orange",
            "title": {
                "tag": "plain_text",
                "content": "⚠️ 申领单超时提醒",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": (
                    f"申领单 REQ-{requisition.id} 已等待 "
                    f"{elapsed} 个工作日。\n"
                    f"物资：{requisition.item_name}\n"
                    "建议联系当前审批人了解进度。"
                ),
            }
        ],
    }

    receivers = {applicant.feishu_open_id}

    if requisition.approver_id:
        approver = await db.get(
            User,
            requisition.approver_id,
        )
        if approver:
            receivers.add(approver.feishu_open_id)

    for open_id in receivers:
        await send_feishu_card(
            open_id,
            card,
            uuid5(
                NAMESPACE_URL,
                f"requisition-warning:"
                f"{requisition.id}:{open_id}",
            ).hex,
        )

    await client.setex(
        warned_key,
        7 * 24 * 60 * 60,
        "1",
    )


async def execute_approval_poll(
    task_id: str,
    *,
    force: bool = False,
) -> dict:
    client = redis_module.redis_client
    lock = client.lock(
        f"dep:cron:lock:{task_id}",
        timeout=60,
        blocking=False,
        thread_local=False,
    )

    if not await lock.acquire():
        return {"status": "LOCKED"}

    try:
        task = await read_task(task_id)
        if not task:
            await client.zrem("dep:cron:queue", task_id)
            return {"status": "MISSING"}

        if (
            not force
            and int(task["execute_at"]) > now_ms()
        ):
            return {"status": "PENDING"}

        payload = task["payload"]
        requisition_id = int(
            payload["requisition_id"]
        )

        async with create_session() as db:
            requisition = await db.get(
                Requisition,
                requisition_id,
            )
            if requisition is None:
                await finish_task(task_id, "FAILED")
                return {"status": "FAILED"}

            age_days = (
                utc_now() - requisition.created_at
            ).total_seconds() / 86400

            if age_days >= payload.get(
                "max_poll_days",
                MAX_POLL_DAYS,
            ):
                await finish_task(task_id, "TIMEOUT")
                return {"status": "TIMEOUT"}

            try:
                status_data = await call_requisition_tool(
                    "query_requisition",
                    {"requisition_id": requisition_id,
                                "user_id": requisition.user_id,
                                "system": True},
                )

            except Exception:
                await _reschedule(
                    task,
                    payload,
                    5 * 60 * 1000,
                )
                return {"status": "RETRY"}

            new_status = status_data["status"]
            old_status = payload.get(
                "last_status",
                requisition.status,
            )
            applicant = await db.get(
                User,
                requisition.user_id,
            )

            if new_status != old_status:
                # 先同步数据库，再发送通知。
                await _sync_local_status(
                    db,
                    requisition,
                    status_data,
                )

                if applicant is not None:
                    await _notify_status_change(
                        requisition,
                        applicant,
                        status_data,
                    )

                payload["last_status"] = new_status
                await client.setex(
                    f"dep:approval:last_status:"
                    f"{requisition_id}",
                    60 * 60,
                    new_status,
                )

            if (
                applicant is not None
                and new_status
                not in TERMINAL_STATUSES
            ):
                await _check_overtime_warning(
                    db,
                    requisition,
                    applicant,
                    status_data,
                )

            payload["poll_count"] = (
                int(payload.get("poll_count", 0)) + 1
            )

            if new_status in TERMINAL_STATUSES:
                await finish_task(task_id)
                return {"status": "COMPLETED"}

            await _reschedule(
                task,
                payload,
                poll_interval_ms(payload["poll_count"]),
            )
            return {
                "status": "PENDING",
                "requisition_status": new_status,
            }
    finally:
        try:
            await lock.release()
        except Exception:
            pass