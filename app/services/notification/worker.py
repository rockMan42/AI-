import asyncio
import logging
from contextlib import suppress
from datetime import datetime, time, timedelta

from app.config.settings import get_settings
from app.services.notification.core import deliver_due
from app.services.notification.scenes import scan_attendance
from app.utils.time import SHANGHAI_TIMEZONE, as_shanghai, utc_now


log = logging.getLogger(__name__)
_task = None


async def due_attendance_days(now):
    start, end = time(9), time(18)
    if get_settings().business_rules_enabled:
        from app.services.business_rules.repository import repository
        snapshot = await repository.get("attendance")
        start = time.fromisoformat(snapshot["rule_data"]["start_time"])
        end = time.fromisoformat(snapshot["rule_data"]["end_time"])
    if end <= start:
        return [now.date() - timedelta(days=1)] if now.time() >= end else []
    return [now.date()] if now.time() >= end else []


async def loop():
    scanned_slots = set()
    while True:
        try:
            await deliver_due()
            local = as_shanghai(utc_now())
            for day in await due_attendance_days(local):
                slot = (day, local.replace(minute=local.minute // 5 * 5, second=0, microsecond=0))
                if slot not in scanned_slots:
                    await scan_attendance(morning=False, work_date=day)
                    scanned_slots.add(slot)
            scanned_slots = {slot for slot in scanned_slots if slot[1] >= local - timedelta(minutes=10)}
        except Exception:
            log.exception("notification_worker_failed")
        await asyncio.sleep(30)


async def start_notification_worker():
    global _task
    if get_settings().notification_enabled and _task is None:
        _task = asyncio.create_task(loop(), name="notification-delivery")


async def stop_notification_worker():
    global _task
    task, _task = _task, None
    if task:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
