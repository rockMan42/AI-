import logging
from datetime import date, datetime, time, timedelta

from sqlalchemy import select
from sqlalchemy.orm import aliased

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.attendance import Attendance
from app.models.department import Department
from app.models.expense import Expense
from app.models.lead import Lead
from app.models.leave_request import LeaveRequest
from app.models.leave_workflow import WorkCalendarDay
from app.models.notification import NotificationLog
from app.models.performance import PerformanceReview
from app.models.requisition import Requisition
from app.models.user import User
from app.security.auth import ACTIVE_STATUSES
from app.services.expense.approval_state import POLL_STATUSES
from app.services.lead import CLOSED_STATUSES, overdue_condition
from app.services.performance import DRAFT_STATUSES
from app.services.performance_reminder import scheduled_reminder_type
from app.utils.time import SHANGHAI_TIMEZONE, as_shanghai, utc_now

from .core import enqueue


log = logging.getLogger(__name__)


def _buttons(kind: str, identifier: str, version: str) -> dict:
    return {"buttons": [{
        "label": label,
        "type": "primary" if operation == "approve" else "default",
        "value": {
            "module": "notification", "operation": operation,
            "kind": kind, "source_id": identifier, "version": version,
        },
    } for label, operation in (("通过", "approve"), ("驳回", "reject_hint"))]}


async def valid_item(db, item) -> bool:
    if item.scene not in {"approval_reminder", "attendance_alert", "lead_follow", "review_deadline"}:
        return True
    receiver = await db.scalar(select(NotificationLog.target_user_id).where(NotificationLog.id == item.log_id))
    now = utc_now()
    if item.scene == "approval_reminder":
        kind, _, identifier = item.source_id.partition(":")
        if kind == "leave":
            row = await db.scalar(select(LeaveRequest).where(LeaveRequest.request_id == identifier))
            return bool(row and row.status == "pending" and row.approver_id == receiver and row.created_at.isoformat() == item.source_version)
        if kind == "expense":
            row = await db.get(Expense, int(identifier))
            return bool(row and row.status in POLL_STATUSES and row.current_approver_id == receiver and str(row.approval_version) == item.source_version and row.approval_synced_at and now - row.approval_synced_at <= timedelta(minutes=15))
        if kind == "requisition":
            row = await db.get(Requisition, int(identifier))
            return bool(row and row.status in {"pending", "approving"} and row.approver_id == receiver and row.node_started_at and row.node_started_at.isoformat() == item.source_version)
        return False
    if item.scene == "lead_follow":
        row = await db.get(Lead, int(item.source_id))
        return bool(row and row.assigned_to == receiver and row.status not in CLOSED_STATUSES and (row.last_follow_up or row.created_at).isoformat() == item.source_version and as_shanghai(now).date() - as_shanghai(row.last_follow_up or row.created_at).date() > timedelta(days=7))
    if item.scene == "review_deadline":
        row = await db.get(PerformanceReview, int(item.source_id))
        return bool(row and row.reviewer_id == receiver and row.status in DRAFT_STATUSES and row.deadline and scheduled_reminder_type(row.deadline, as_shanghai(now).date()) == item.source_version)
    row = await db.get(Attendance, int(item.source_id.split(":")[0]))
    return bool(row and row.date.isoformat() == item.source_version)


async def scan_approval() -> int:
    now = utc_now()
    threshold = now - timedelta(hours=48)
    events = []
    async with create_session() as db:
        leaves = list(await db.scalars(select(LeaveRequest).where(
            LeaveRequest.status == "pending", LeaveRequest.approver_id.is_not(None),
            LeaveRequest.created_at < threshold, LeaveRequest.request_id.is_not(None),
        )))
        for row in leaves:
            events.append(("leave", str(row.request_id), row.created_at.isoformat(), row.approver_id))
        expenses = list(await db.scalars(select(Expense).where(
            Expense.status.in_(POLL_STATUSES), Expense.current_approver_id.is_not(None),
            Expense.node_started_at < threshold, Expense.approval_synced_at >= now - timedelta(minutes=15),
        )))
        for row in expenses:
            events.append(("expense", str(row.id), str(row.approval_version), row.current_approver_id))
        requisitions = list(await db.scalars(select(Requisition).where(
            Requisition.status.in_(("pending", "approving")), Requisition.approver_id.is_not(None),
            Requisition.node_started_at < threshold,
        )))
        for row in requisitions:
            events.append(("requisition", str(row.id), row.node_started_at.isoformat(), row.approver_id))
    count = 0
    for kind, identifier, version, receiver in events:
        count += bool(await enqueue(
            scene="approval_reminder", source_id=f"{kind}:{identifier}",
            source_version=version, target_user_id=receiver,
            variables={"kind": {"leave": "请假", "expense": "报销", "requisition": "申领"}[kind], "identifier": identifier},
            action=_buttons(kind, identifier, version),
        ))
    return count


async def scan_leads() -> int:
    now = utc_now()
    async with create_session() as db:
        rows = list(await db.scalars(select(Lead).where(
            overdue_condition(now), Lead.assigned_to.is_not(None),
        )))
    count = 0
    for row in rows:
        count += bool(await enqueue(
            scene="lead_follow", source_id=str(row.id),
            source_version=(row.last_follow_up or row.created_at).isoformat(),
            target_user_id=row.assigned_to,
            variables={"company_name": row.company_name, "days_idle": (as_shanghai(now).date() - as_shanghai(row.last_follow_up or row.created_at).date()).days},
        ))
    return count


async def scan_reviews() -> int:
    today = as_shanghai(utc_now()).date()
    async with create_session() as db:
        rows = list(await db.scalars(select(PerformanceReview).where(
            PerformanceReview.status.in_(DRAFT_STATUSES),
            PerformanceReview.reviewer_id.is_not(None),
            PerformanceReview.deadline.in_((today + timedelta(days=1), today + timedelta(days=3))),
        )))
    count = 0
    for row in rows:
        level = scheduled_reminder_type(row.deadline, today)
        count += bool(await enqueue(
            scene="review_deadline", source_id=str(row.id),
            source_version=level, target_user_id=row.reviewer_id,
            variables={"period": row.period, "deadline": row.deadline.isoformat(), "days_left": (row.deadline - today).days},
        ))
    return count


async def scan_attendance(*, morning: bool, work_date: date | None = None) -> int:
    now = as_shanghai(utc_now())
    today = work_date or now.date()
    async with create_session() as db:
        workday = await db.get(WorkCalendarDay, today)
        if workday is None or not workday.is_workday:
            return 0
        rows = list(await db.scalars(select(Attendance).where(Attendance.date == today)))
        users = {u.user_id: u for u in await db.scalars(select(User).where(User.user_id.in_([r.user_id for r in rows])))}
        departments = {d.department_id: d for d in await db.scalars(select(Department))}
        from datetime import UTC
        start_utc = datetime.combine(today, time.min, SHANGHAI_TIMEZONE).astimezone(UTC).replace(tzinfo=None)
        end_utc = (datetime.combine(today, time.min, SHANGHAI_TIMEZONE) + timedelta(days=2)).astimezone(UTC).replace(tzinfo=None)
        approved_leaves = list(await db.scalars(select(LeaveRequest).where(
            LeaveRequest.status == "approved",
            LeaveRequest.start_time < end_utc, LeaveRequest.end_time >= start_utc,
        )))
    snapshot = None
    if get_settings().business_rules_enabled:
        from app.services.business_rules.repository import repository
        snapshot = await repository.get("attendance")
    events = []
    for row in rows:
        user = users.get(row.user_id)
        if user is None or user.status not in ACTIVE_STATUSES:
            continue
        rule = snapshot["rule_data"] if snapshot else None
        shift_start = datetime.combine(today, time.fromisoformat(rule["start_time"]), SHANGHAI_TIMEZONE) if rule else datetime.combine(today, time(9), SHANGHAI_TIMEZONE)
        shift_end = datetime.combine(today, time.fromisoformat(rule["end_time"]), SHANGHAI_TIMEZONE) if rule else datetime.combine(today, time(18), SHANGHAI_TIMEZONE)
        if shift_end <= shift_start:
            shift_end += timedelta(days=1)
        full_leave = any(leave.user_id == row.user_id and as_shanghai(leave.start_time) <= shift_start and as_shanghai(leave.end_time) >= shift_end for leave in approved_leaves)
        partial_leave = any(leave.user_id == row.user_id and as_shanghai(leave.start_time) < shift_end and as_shanghai(leave.end_time) > shift_start for leave in approved_leaves)
        if full_leave or partial_leave:
            continue
        if not morning and now < shift_end:
            continue
        if snapshot:
            from app.services.business_rules.evaluators import evaluate_snapshot
            result = evaluate_snapshot(snapshot, {
                "work_date": today.isoformat(), "clock_in_time": row.clock_in_time,
                "clock_out_time": row.clock_out_time, "now": utc_now(),
                "is_workday": True, "full_day_leave": full_leave, "partial_leave": partial_leave,
            })
            issues = set(result["issues"])
        else:
            issues = {"late" if row.status in {"late", "迟到"} else "early_leave" if row.status in {"early_leave", "早退"} else "absent" if row.status in {"absent", "缺勤"} else ""}
        if morning:
            issues &= {"late"}
        else:
            issues &= {"early_leave", "absent"}
        for issue in issues:
            events.append((row, user, issue))

    count = 0
    for row, user, issue in events:
        receivers = {user.user_id: "本人"}
        department = departments.get(user.department_id)
        while department is not None:
            if department.manager_user_id and department.manager_user_id != user.user_id:
                receivers[department.manager_user_id] = "主管"
                break
            department = departments.get(department.parent_id)
        for receiver, role in receivers.items():
            count += bool(await enqueue(
                scene="attendance_alert", source_id=f"{row.id}:{issue}",
                source_version=today.isoformat(), target_user_id=receiver,
                variables={"employee_name": user.name, "issue": {"late": "迟到", "early_leave": "早退", "absent": "缺勤"}[issue], "date": today.isoformat(), "role": role},
            ))
    return count
