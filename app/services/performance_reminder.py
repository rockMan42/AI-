import asyncio
import logging
from contextlib import suppress
from datetime import date, timedelta
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.performance import (
    PerformanceReminderLog,
    PerformanceReview,
)
from app.models.user import User
from app.schemas.permission import Principal
from app.security.auth import ACTIVE_STATUSES
from app.security.permission import authorize
from app.services.notification.legacy import send_feishu_card
from app.services.holiday_cron import lease
from app.services.performance import (
    DRAFT_STATUSES,
    FINAL_STATUSES,
    PerformanceService,
)
from app.utils.time import as_shanghai, utc_now


log = logging.getLogger(__name__)
_worker = None


async def refresh_performance_snapshots() -> dict:
    async with lease("dep:performance:stats"):
        async with create_session() as db:
            cycles = list(await db.scalars(
                select(PerformanceReview.period)
                .where(PerformanceReview.status.in_(FINAL_STATUSES))
                .distinct()
            ))
            service = PerformanceService()
            refreshed = 0
            for cycle in cycles:
                for dimension in ("department", "level", "tenure"):
                    await service.refresh_stats(db, cycle, dimension)
                    refreshed += 1
            return {"cycles": len(cycles), "snapshots": refreshed}


def scheduled_reminder_type(deadline: date, today: date) -> str | None:
    days_left = (deadline - today).days
    if days_left == 3:
        return "DEADLINE_3D"
    if days_left == 1:
        return "DEADLINE_1D"
    return None


def reminder_card(review, reviewee_name: str, days_left: int) -> dict:
    urgent = days_left <= 1
    return {
        "header": {
            "template": "red" if urgent else "orange",
            "title": {
                "tag": "plain_text",
                "content": "绩效考核紧急催办" if urgent else "绩效考核催办",
            },
        },
        "elements": [{
            "tag": "markdown",
            "content": (
                f"**被评人：** {reviewee_name}\n"
                f"**考核周期：** {review.period}\n"
                f"**截止日期：** {review.deadline.isoformat()}\n"
                f"**剩余天数：** {days_left} 天"
            ),
        }],
    }


async def pending_reminders(
    db,
    principal: Principal,
    today: date | None = None,
) -> list[dict]:
    await authorize(principal, "performance.read", scope="team")
    current = today or date.today()
    statement = (
        select(PerformanceReview, User.name)
        .join(User, User.user_id == PerformanceReview.user_id)
        .where(
            PerformanceReview.status.in_(DRAFT_STATUSES),
            PerformanceReview.deadline.is_not(None),
            PerformanceReview.deadline.between(
                current,
                current + timedelta(days=3),
            ),
        )
        .order_by(PerformanceReview.deadline, PerformanceReview.id)
    )
    if principal.role.value == "Manager":
        statement = statement.where(
            PerformanceReview.reviewer_id == principal.user_id
        )
    rows = (await db.execute(statement)).all()
    return [
        {
            "review_id": review.id,
            "user_id": review.user_id,
            "user_name": name,
            "reviewer_id": review.reviewer_id,
            "cycle": review.period,
            "deadline": review.deadline.isoformat(),
            "days_left": (review.deadline - current).days,
        }
        for review, name in rows
    ]


async def send_reminders(
    db,
    principal: Principal,
    review_ids: list[int] | None = None,
    *,
    today: date | None = None,
    manual: bool = True,
    sender=send_feishu_card,
    check_permission: bool = True,
) -> dict:
    if check_permission:
        await authorize(principal, "performance.remind")
    current = today or date.today()
    reviewer = aliased(User)
    reviewee = aliased(User)
    statement = (
        select(
            PerformanceReview,
            reviewer.feishu_open_id,
            reviewee.name,
        )
        .join(
            reviewer,
            reviewer.user_id == PerformanceReview.reviewer_id,
        )
        .join(
            reviewee,
            reviewee.user_id == PerformanceReview.user_id,
        )
        .where(
            PerformanceReview.status.in_(DRAFT_STATUSES),
            PerformanceReview.deadline.is_not(None),
            reviewer.status.in_(ACTIVE_STATUSES),
        )
    )
    if review_ids is not None:
        if not review_ids:
            return {"sent": 0, "failed": 0}
        statement = statement.where(PerformanceReview.id.in_(review_ids))
    elif manual:
        statement = statement.where(
            PerformanceReview.deadline.between(
                current,
                current + timedelta(days=3),
            )
        )
    else:
        statement = statement.where(
            PerformanceReview.deadline.in_({
                current + timedelta(days=1),
                current + timedelta(days=3),
            })
        )

    rows = (await db.execute(statement)).all()
    sent = 0
    failed = 0
    for review, open_id, reviewee_name in rows:
        reminder_type = (
            "MANUAL"
            if manual
            else scheduled_reminder_type(review.deadline, current)
        )
        if reminder_type is None:
            continue
        if not manual:
            existing = await db.scalar(
                select(PerformanceReminderLog.id).where(
                    PerformanceReminderLog.review_id == review.id,
                    PerformanceReminderLog.reminder_type == reminder_type,
                    PerformanceReminderLog.status == "SENT",
                )
            )
            if existing is not None:
                continue

        days_left = (review.deadline - current).days
        status = "SENT"
        try:
            await sender(
                open_id,
                reminder_card(review, reviewee_name, days_left),
                str(uuid5(
                    NAMESPACE_URL,
                    f"performance:{review.id}:{reminder_type}:{current}",
                )),
            )
            sent += 1
        except Exception:
            log.exception(
                "performance_reminder_failed review_id=%s",
                review.id,
            )
            status = "FAILED"
            failed += 1

        db.add(PerformanceReminderLog(
            review_id=review.id,
            reminder_type=reminder_type,
            target_user_id=review.reviewer_id,
            channel="FEISHU",
            status=status,
        ))

    await db.commit()
    return {"sent": sent, "failed": failed}


async def run_scheduled_reminders(today: date | None = None) -> dict:
    from app.schemas.permission import Principal, Role

    current = today or date.today()
    async with lease(f"dep:performance:reminders:{current}"):
        async with create_session() as db:
            # 内部 Worker 不使用外部用户令牌，该主体只用于调用签名。
            principal = Principal(
                user_id=1,
                open_id="internal:performance-reminder",
                role=Role.HR_ADMIN,
                department_id=0,
                source="manual",
                version=0,
            )
            return await send_reminders(
                db,
                principal,
                today=current,
                manual=False,
                check_permission=False,
            )


async def worker_loop():
    stats_date = None
    reminder_date = None
    while True:
        now = as_shanghai(utc_now())
        try:
            if stats_date != now.date():
                await refresh_performance_snapshots()
                stats_date = now.date()
            if reminder_date != now.date() and now.hour >= 9:
                await run_scheduled_reminders(now.date())
                reminder_date = now.date()
        except Exception:
            log.exception("performance_reminder_worker_failed")
        await asyncio.sleep(
            get_settings().performance_reminder_poll_seconds
        )


async def start_performance_reminder_worker():
    global _worker
    if not get_settings().performance_reminder_enabled or get_settings().notification_enabled:
        return
    if _worker is None:
        _worker = asyncio.create_task(
            worker_loop(),
            name="performance-reminder-worker",
        )


async def stop_performance_reminder_worker():
    global _worker
    task, _worker = _worker, None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
