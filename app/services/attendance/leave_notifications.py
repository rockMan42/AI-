import asyncio
import json
import logging
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select, update

from app.core.database import create_session
from app.models.leave_request import LeaveRequest
from app.models.leave_workflow import LeaveNotification
from app.models.user import User
from app.security.auth import ACTIVE_STATUSES
from app.services.attendance.leave_calendar import is_overdue, load_calendar
from app.services.attendance.leave_service import LeaveError, LeaveService
from app.services.conversation_engine.feishu import (
    FEISHU_API_BASE,
    _get_tenant_access_token,
    get_feishu_client,
    wait_feishu_send_slot,
)
from app.utils.time import as_shanghai, utc_now


log = logging.getLogger(__name__)
_worker = None
_stop = None


async def track_leave_approvals() -> int:
    from app.config.settings import get_settings
    if get_settings().notification_enabled:
        return 0
    service = LeaveService()
    last_id = 0
    escalated = 0

    while True:
        async with create_session() as db:
            rows = (
                await db.execute(
                    select(LeaveRequest.id, LeaveRequest.request_id)
                    .where(
                        LeaveRequest.id > last_id,
                        LeaveRequest.request_id.is_not(None),
                        LeaveRequest.status == "pending",
                        LeaveRequest.escalated_at.is_(None),
                    )
                    .order_by(LeaveRequest.id)
                    .limit(100)
                )
            ).all()

        if not rows:
            return escalated

        for numeric_id, request_id in rows:
            last_id = numeric_id
            try:
                async with create_session() as db:
                    async with db.begin():
                        row = await service._locked_request(db, request_id)
                        if row.status != "pending" or row.escalated_at:
                            continue
                        if not await is_overdue(db, row.created_at, utc_now()):
                            continue

                        approver = await db.get(User, row.approver_id)
                        if (
                            approver is None
                            or approver.status not in ACTIVE_STATUSES
                        ):
                            raise LeaveError("审批人不存在或已停用")

                        receivers = {approver.user_id: approver}
                        try:
                            supervisor = await service._manager(db, approver)
                            if supervisor.user_id != row.user_id:
                                receivers[supervisor.user_id] = supervisor
                        except LeaveError:
                            # 审批人的提醒仍发送；组织配置异常留运维日志。
                            log.warning(
                                "leave_supervisor_missing request_id=%s",
                                request_id,
                            )

                        row.escalated_at = utc_now()
                        row.updated_at = utc_now()
                        service._record(
                            db, row, "escalate", None, "待审批超过48小时",
                        )
                        for receiver in receivers.values():
                            service._enqueue(
                                db, row, receiver, "overdue",
                                {
                                    "msg_type": "text",
                                    "content": {
                                        "text": (
                                            f"请假申请 {request_id} "
                                            "待审批已超48小时，请尽快处理。"
                                        )
                                    },
                                },
                            )
                    escalated += 1
            except Exception as exc:
                # 不输出SQL参数、理由或异常正文。
                log.error(
                    "leave_tracker_failed request_id=%s error_type=%s",
                    request_id,
                    type(exc).__name__,
                )


async def _claim_notification():
    now = utc_now()

    async with create_session() as db:
        async with db.begin():
            item = await db.scalar(
                select(LeaveNotification)
                .where(
                    LeaveNotification.status.in_(("pending", "sending")),
                    LeaveNotification.next_attempt_at <= now,
                )
                .order_by(LeaveNotification.id)
                .limit(1)
                .with_for_update(skip_locked=True)
            )
            if item is None:
                return None

            # 飞书发送请求去重有时间窗口，不能无限重试后仍声称绝不重复。
            # 超过保守窗口的未知投递转人工核对，不盲目重发。
            if (
                item.first_attempt_at is not None
                and now - item.first_attempt_at >= timedelta(minutes=50)
            ):
                item.status = "unknown"
                log.error(
                    "leave_notification_needs_review notification_id=%s",
                    item.id,
                )
                return {}

            row = await db.scalar(
                select(LeaveRequest).where(
                    LeaveRequest.request_id == item.request_id,
                )
            )
            if row is None:
                item.status = "unknown"
                return {}

            # 已完成的申请不再发送旧的待审批/催办消息。
            if item.kind in {"submitted", "overdue"} and row.status != "pending":
                item.status = "skipped"
                return {}

            if item.kind == "overdue":
                today = as_shanghai(now).date()
                calendar = await load_calendar(db, today, today)
                if not calendar[today]:
                    item.next_attempt_at = now + timedelta(minutes=30)
                    return {}

            lease_token = uuid4().hex
            item.status = "sending"
            item.lease_token = lease_token
            item.attempts += 1
            item.first_attempt_at = item.first_attempt_at or now
            item.next_attempt_at = now + timedelta(seconds=60)

            return {
                "id": item.id,
                "lease_token": lease_token,
                "event_key": item.event_key,
                "receiver_open_id": item.receiver_open_id,
                "payload": item.payload,
            }


async def deliver_notifications(limit: int = 100) -> None:
    for _ in range(limit):
        item = await _claim_notification()
        if item is None:
            return
        if not item:
            continue

        sent = False
        message_id = None

        try:
            async with asyncio.timeout(15):
                from app.config.settings import get_settings
                if get_settings().notification_enabled:
                    from app.services.notification.legacy import send_feishu_card
                    content = item["payload"]["content"]
                    card = content if item["payload"]["msg_type"] == "interactive" else {
                        "header": {"title": {"tag": "plain_text", "content": "请假通知"}},
                        "elements": [{"tag": "markdown", "content": content.get("text", "请查看请假通知")}],
                    }
                    message_id = await send_feishu_card(
                        item["receiver_open_id"], card,
                        uuid5(NAMESPACE_URL, "leave:" + item["event_key"]).hex,
                    )
                else:
                    token = await _get_tenant_access_token()
                    await wait_feishu_send_slot()
                    response = await get_feishu_client().post(
                        f"{FEISHU_API_BASE}/im/v1/messages",
                        params={"receive_id_type": "open_id"},
                        headers={"Authorization": f"Bearer {token}"},
                        json={
                            "receive_id": item["receiver_open_id"],
                            "msg_type": item["payload"]["msg_type"],
                            "content": json.dumps(item["payload"]["content"], ensure_ascii=False),
                            "uuid": uuid5(NAMESPACE_URL, "leave:" + item["event_key"]).hex,
                        },
                    )
                    response.raise_for_status()
                    payload = response.json()
                    if payload.get("code") != 0:
                        raise RuntimeError("飞书通知发送失败")
                    message_id = payload.get("data", {}).get("message_id")
                sent = True
        except Exception as exc:
            log.warning(
                "leave_notification_failed notification_id=%s error_type=%s",
                item["id"],
                type(exc).__name__,
            )

        async with create_session() as db:
            async with db.begin():
                values = {
                    "status": "sent" if sent else "pending",
                    "lease_token": None,
                    "next_attempt_at": utc_now() + timedelta(seconds=30),
                    "updated_at": utc_now(),
                }
                if sent:
                    values.update(sent_at=utc_now(), message_id=message_id)

                await db.execute(
                    update(LeaveNotification)
                    .where(
                        LeaveNotification.id == item["id"],
                        LeaveNotification.status == "sending",
                        LeaveNotification.lease_token == item["lease_token"],
                    )
                    .values(**values)
                )


async def _notification_loop():
    while not _stop.is_set():
        try:
            await deliver_notifications()
        except Exception as exc:
            log.error(
                "leave_notification_worker_failed error_type=%s",
                type(exc).__name__,
            )

        try:
            await asyncio.wait_for(_stop.wait(), timeout=5)
        except TimeoutError:
            pass


async def start_leave_notification_worker():
    global _worker, _stop
    if _worker is not None:
        return
    _stop = asyncio.Event()
    _worker = asyncio.create_task(
        _notification_loop(),
        name="leave-notifications",
    )


async def stop_leave_notification_worker():
    global _worker, _stop
    if _worker is None:
        return
    _stop.set()
    await _worker
    _worker = None
    _stop = None
