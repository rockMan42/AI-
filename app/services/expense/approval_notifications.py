import asyncio
import logging
from datetime import timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select

from app.core.database import create_session
from app.models import Expense, User
from app.models.expense_approval import (
    ExpenseApprovalLog,
    ExpenseNotification,
)
from app.security.auth import ACTIVE_STATUSES
from app.services.notification.legacy import send_feishu_card
from app.services.expense.approval_cards import timeout_card
from app.services.expense.approval_state import POLL_STATUSES
from app.services.holiday_cron import HolidayError, lease
from app.utils.time import as_shanghai, utc_now


log = logging.getLogger(__name__)


async def prepare_delivery(notification_id):
    async with create_session() as db, db.begin():
        item = await db.scalar(
            select(ExpenseNotification)
            .where(ExpenseNotification.id == notification_id)
            .with_for_update()
        )
        now = utc_now()

        if (
            item is None
            or item.status not in {"pending", "sending"}
            or item.next_attempt_at > now
        ):
            return None

        if (
            item.first_attempt_at is not None
            and now - item.first_attempt_at >= timedelta(minutes=50)
        ):
            item.status = "unknown"
            item.last_error = "delivery_requires_review"
            log.error(
                "expense_notification_needs_review notification_id=%s",
                item.id,
            )
            return None

        user = await db.get(User, item.receiver_id)
        if (
            user is None
            or user.status not in ACTIVE_STATUSES
            or not user.feishu_open_id
        ):
            item.next_attempt_at = now + timedelta(minutes=5)
            item.last_error = "receiver_unavailable"
            return None

        payload = dict(item.payload)

        if item.kind == "timeout":
            if payload["day"] != as_shanghai(now).date().isoformat():
                item.status = "skipped"
                return None

            valid = []
            for entry in payload["items"]:
                expense = await db.get(Expense, entry["expense_id"])
                if (
                    expense is not None
                    and expense.status in POLL_STATUSES
                    and expense.status == entry["status"]
                    and expense.approval_version == entry["version"]
                    and expense.current_approver_id == item.receiver_id
                    and expense.approval_synced_at is not None
                    and now - expense.approval_synced_at
                    <= timedelta(minutes=15)
                ):
                    valid.append(entry)

            if not valid:
                item.status = "skipped"
                return None

            # 已经尝试发送的消息不能换内容后继续复用相同 UUID。
            if (
                item.first_attempt_at is not None
                and len(valid) != len(payload["items"])
            ):
                item.status = "skipped"
                return None

            if item.first_attempt_at is None:
                payload["items"] = valid
                payload["card"] = timeout_card(valid)
                item.payload = payload

        item.first_attempt_at = item.first_attempt_at or now
        item.status = "sending"
        item.attempts += 1
        item.next_attempt_at = now + timedelta(seconds=30)

        return {
            "id": item.id,
            "open_id": user.feishu_open_id,
            "card": payload["card"],
            "uuid": uuid5(NAMESPACE_URL, item.event_key).hex,
        }


async def deliver_one(notification_id):
    async with lease(f"dep:expense:notification:{notification_id}"):
        target = await prepare_delivery(notification_id)
        if target is None:
            return

        try:
            async with asyncio.timeout(15):
                message_id = await send_feishu_card(
                    target["open_id"],
                    target["card"],
                    target["uuid"],
                )
        except Exception as exc:
            async with create_session() as db, db.begin():
                item = await db.get(
                    ExpenseNotification, notification_id,
                )
                item.status = "pending"
                item.last_error = type(exc).__name__
                item.next_attempt_at = (
                    utc_now() + timedelta(seconds=30)
                )
            return

        async with create_session() as db, db.begin():
            item = await db.scalar(
                select(ExpenseNotification)
                .where(ExpenseNotification.id == notification_id)
                .with_for_update()
            )
            item.status = "sent"
            item.sent_at = utc_now()
            item.message_id = message_id
            item.last_error = None

            if item.approval_log_id:
                event = await db.get(
                    ExpenseApprovalLog, item.approval_log_id,
                )
                event.notified = True
                event.notified_at = item.sent_at

                if event.version == 0:
                    expense = await db.get(Expense, event.expense_id)
                    expense.notified = True


async def deliver_pending():
    async with create_session() as db:
        ids = list((await db.scalars(
            select(ExpenseNotification.id)
            .where(
                ExpenseNotification.status.in_(["pending", "sending"]),
                ExpenseNotification.next_attempt_at <= utc_now(),
            )
            .order_by(
                ExpenseNotification.next_attempt_at,
                ExpenseNotification.id,
            )
            .limit(100)
        )).all())

    for notification_id in ids:
        try:
            await deliver_one(notification_id)
        except HolidayError:
            # 其他实例持有相同通知的锁。
            continue
        except Exception as exc:
            log.error(
                "expense_notification_failed "
                "notification_id=%s error_type=%s",
                notification_id,
                type(exc).__name__,
            )
