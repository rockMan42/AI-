from sqlalchemy import select

from app.core.database import create_session
from app.models.attendance import Attendance
from app.models.leave_request import LeaveRequest
from app.models.leave_workflow import WorkCalendarDay
from app.schemas.attendance import month_bounds
from app.services.business_rules.repository import repository
from app.services.business_rules.evaluators import evaluate_snapshot
from app.utils.time import as_shanghai, utc_now


async def recalculate_month(request, user_id, session_factory=create_session):
    snapshot = await repository.get("attendance")
    start, end = month_bounds(request.month)
    async with session_factory() as db:
        rows = list(
            await db.scalars(
                select(Attendance)
                .where(
                    Attendance.user_id == user_id,
                    Attendance.date >= start,
                    Attendance.date < end,
                )
                .order_by(Attendance.date.desc(), Attendance.id.desc())
            )
        )
        calendar = {
            r.day: r.is_workday
            for r in await db.scalars(
                select(WorkCalendarDay).where(
                    WorkCalendarDay.day >= start, WorkCalendarDay.day < end
                )
            )
        }
        # 扩一天覆盖月末跨夜班次，请假时间按 UTC 数据库约定比较。
        from datetime import datetime, time, timedelta
        from zoneinfo import ZoneInfo

        zone = ZoneInfo("Asia/Shanghai")
        lower = (
            datetime.combine(start, time.min, zone)
            .astimezone(ZoneInfo("UTC"))
            .replace(tzinfo=None)
        )
        upper = (
            datetime.combine(end + timedelta(days=1), time.min, zone)
            .astimezone(ZoneInfo("UTC"))
            .replace(tzinfo=None)
        )
        leaves = list(
            await db.scalars(
                select(LeaveRequest).where(
                    LeaveRequest.user_id == user_id,
                    LeaveRequest.status == "approved",
                    LeaveRequest.start_time < upper,
                    LeaveRequest.end_time >= lower,
                )
            )
        )
    now = utc_now()
    items = []
    for row in rows:
        shift_start = datetime.combine(
            row.date, time.fromisoformat(snapshot["rule_data"]["start_time"]), zone
        )
        shift_end = datetime.combine(
            row.date, time.fromisoformat(snapshot["rule_data"]["end_time"]), zone
        )
        if shift_end <= shift_start:
            shift_end += timedelta(days=1)
        full = any(
            as_shanghai(l.start_time) <= shift_start
            and as_shanghai(l.end_time) >= shift_end
            for l in leaves
        )
        partial = not full and any(
            as_shanghai(l.start_time) < shift_end
            and as_shanghai(l.end_time) > shift_start
            for l in leaves
        )
        computed = evaluate_snapshot(
            snapshot,
            {
                "work_date": row.date.isoformat(),
                "clock_in_time": row.clock_in_time,
                "clock_out_time": row.clock_out_time,
                "now": now,
                "is_workday": calendar.get(row.date),
                "full_day_leave": full,
                "partial_leave": partial,
            },
        )
        items.append(
            {
                "date": row.date.isoformat(),
                "punch_in": as_shanghai(row.clock_in_time).strftime("%H:%M")
                if row.clock_in_time
                else None,
                "punch_out": as_shanghai(row.clock_out_time).strftime("%H:%M")
                if row.clock_out_time
                else None,
                "source_status": row.status,
                "work_hours": str(row.work_hours)
                if row.work_hours is not None
                else None,
                **computed,
                "calculated_at": as_shanghai(now).isoformat(),
                "calculation_mode": "latest_rule",
            }
        )
    stats = {
        "record_count": len(items),
        "late_count": sum("late" in i["issues"] for i in items),
        "early_leave_count": sum("early_leave" in i["issues"] for i in items),
        "calculated_at": as_shanghai(now).isoformat(),
        "rule_version": snapshot["version"],
        "calculation_mode": "latest_rule",
        "cached": False,
    }
    filtered = [
        i
        for i in items
        if (request.query_date is None or i["date"] == request.query_date.isoformat())
        and (
            request.status_filter is None
            or i["status"] == request.status_filter
            or request.status_filter in i["issues"]
        )
    ]
    records = {
        "items": filtered[request.offset : request.offset + request.limit],
        "limit": request.limit,
        "offset": request.offset,
        "has_more": len(filtered) > request.offset + request.limit,
    }
    return {
        "punch_records": records,
        "late_stats": stats,
        "rule_version": snapshot["version"],
        "calculation_mode": "latest_rule",
    }
