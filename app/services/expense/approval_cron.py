import logging
from datetime import datetime, time, timedelta

from app.services.holiday_cron import (
    QUEUE_KEY,
    cache,
    lease,
    now_ms,
    read_task,
    register_task,
    task_key,
)
from app.utils.time import as_shanghai, utc_now


log = logging.getLogger(__name__)

TASKS = {
    "EXPENSE_APPROVAL_POLL",
    "EXPENSE_TIMEOUT_SCAN",
    "EXPENSE_NOTIFICATION_DELIVERY",
}


def next_nine_ms() -> int:
    now = as_shanghai(utc_now())
    target = datetime.combine(
        now.date(), time(9), tzinfo=now.tzinfo,
    )
    if target <= now:
        target += timedelta(days=1)
    return int(target.timestamp() * 1000)


async def reconcile_tasks():
    now = as_shanghai(utc_now())
    today_nine = datetime.combine(
        now.date(), time(9), tzinfo=now.tzinfo,
    )
    # 服务上午九点后启动时，立即补做当天扫描。
    scan_at = max(
        now_ms(),
        int(today_nine.timestamp() * 1000),
    )

    for task_type, execute_at in (
        ("EXPENSE_APPROVAL_POLL", now_ms()),
        ("EXPENSE_TIMEOUT_SCAN", scan_at),
        ("EXPENSE_NOTIFICATION_DELIVERY", now_ms()),
    ):
        await register_task(
            task_type,
            task_type,
            {},
            execute_at,
            replace=False,
        )


async def reschedule(task_id, execute_at):
    async with cache().pipeline(transaction=True) as pipe:
        pipe.hset(
            task_key(task_id),
            mapping={
                "status": "PENDING",
                "execute_at": str(execute_at),
            },
        )
        pipe.zadd(QUEUE_KEY, {task_id: execute_at})
        await pipe.execute()


async def execute_expense_task(task_id, *, force=False):
    from app.services.expense.approval_notifications import (
        deliver_pending,
    )
    from app.services.expense.approval_service import (
        poll_all,
        scan_timeouts,
    )

    async with lease(f"dep:expense:cron:{task_id}"):
        task = await read_task(task_id)
        if not task:
            return {"status": "MISSING"}
        if not force and int(task["execute_at"]) > now_ms():
            return {"status": "PENDING"}

        try:
            if task["task_type"] == "EXPENSE_APPROVAL_POLL":
                await poll_all()
                # 对齐到下一个十分钟边界。
                next_at = (now_ms() // 600_000 + 1) * 600_000
            elif task["task_type"] == "EXPENSE_TIMEOUT_SCAN":
                # 告警前先更新审批人和终态，避免凭昨天的数据催办。
                await poll_all()
                await scan_timeouts()
                next_at = next_nine_ms()
            else:
                await deliver_pending()
                next_at = now_ms() + 30_000
        except Exception as exc:
            log.error(
                "expense_cron_failed task_id=%s error_type=%s",
                task_id,
                type(exc).__name__,
            )
            await reschedule(task_id, now_ms() + 60_000)
            return {"status": "RETRY"}

        await reschedule(task_id, next_at)
        return {"status": "PENDING"}