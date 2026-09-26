import logging
from collections import defaultdict
from datetime import timedelta

from sqlalchemy import select

from app.core.database import create_session
from app.hermes.finance_mcp_client import call_finance_tool
from app.models import Expense, User
from app.models.expense_approval import (
    ExpenseApprovalLog,
    ExpenseNotification,
)
from app.security.auth import ACTIVE_STATUSES
from app.services.attendance.leave_calendar import (
    CalendarUnavailable,
    load_calendar,
)
from app.services.expense.approval_cards import (
    status_card,
    timeout_card,
)
from app.services.expense.approval_state import (
    POLL_STATUSES,
    STATUS_LABELS,
    TERMINAL_STATUSES,
    ensure_transition,
    parse_finance_time,
    working_seconds,
)
from app.services.expense.expense_service import owned_expense
from app.services.expense.rules import money
from app.utils.time import as_shanghai, utc_now


log = logging.getLogger(__name__)


async def enqueue_status(db, expense, event):
    await db.flush()
    db.add(ExpenseNotification(
        event_key=f"expense-status:{expense.id}:{event.version}",
        approval_log_id=event.id,
        receiver_id=expense.user_id,
        kind="status",
        payload={"card": status_card(expense, event)},
        status="pending",
        next_attempt_at=utc_now(),
    ))


async def record_submission(db, expense):
    event = ExpenseApprovalLog(
        expense_id=expense.id,
        version=0,
        from_status="submitting",
        to_status="submitted",
        approver_id=None,
        approver_name=None,
        comment="财务系统已接收报销单",
        occurred_at=expense.submitted_at,
        detail={},
        notified=False,
    )
    db.add(event)
    await enqueue_status(db, expense, event)


async def validate_actor(db, user_id, applicant_id=None):
    if type(user_id) is not int or user_id <= 0:
        raise ValueError("审批人编号无效")
    user = await db.get(User, user_id)
    if (
        user is None
        or user.status not in ACTIVE_STATUSES
        or user.user_id == applicant_id
    ):
        raise ValueError("审批人不存在、已停用或与申请人相同")
    return user


async def sync_one(expense_id: int):
    async with create_session() as db:
        expense = await db.get(Expense, expense_id)
        if expense is None or expense.status not in POLL_STATUSES:
            return
        user_id = expense.user_id

    snapshot = await call_finance_tool(
        "query_expense_status", user_id, expense_id,
    )

    if (
        snapshot.get("expense_id") != expense_id
        or type(snapshot.get("version")) is not int
        or not isinstance(snapshot.get("events"), list)
    ):
        raise ValueError("财务审批响应格式错误")

    async with create_session() as db, db.begin():
        expense = await db.scalar(
            select(Expense)
            .where(Expense.id == expense_id)
            .with_for_update()
        )
        if expense.status not in POLL_STATUSES:
            return

        # 较旧响应不能覆盖已经同步的新版本。
        if snapshot["version"] < expense.approval_version:
            return

        events = snapshot["events"]
        for item in events:
            if not isinstance(item, dict):
                raise ValueError("审批事件格式错误")
            if type(item.get("version")) is not int:
                raise ValueError("审批版本无效")

        versions = [item["version"] for item in events]
        if versions != list(range(1, snapshot["version"] + 1)):
            raise ValueError("财务审批历史不完整或存在重复版本")

        for item in events:
            if item["version"] <= expense.approval_version:
                continue

            if item["version"] != expense.approval_version + 1:
                raise ValueError("审批事件不连续")
            if item.get("from_status") != expense.status:
                raise ValueError("审批事件与当前状态不一致")

            new_status = item.get("to_status")
            ensure_transition(expense.status, new_status)

            occurred_at = parse_finance_time(item["occurred_at"])
            if occurred_at > utc_now() + timedelta(minutes=5):
                raise ValueError("审批时间异常")
            if (
                expense.node_started_at
                and occurred_at < expense.node_started_at
            ):
                raise ValueError("审批时间发生倒退")

            # 历史操作者可能已离职，不要求当前仍启用。
            actor_id = item.get("approver_id")
            if type(actor_id) is not int:
                raise ValueError("审批操作人编号无效")
            route = (expense.payload or {}).get("approval_route")
            if route:
                stage = {"submitted": 0, "manager_approved": 1, "finance_approved": 2}.get(expense.status)
                if stage is None or actor_id != route["nodes"][stage]["user_id"]:
                    raise ValueError("财务审批事件与固定审批路线不一致")
            actor = await db.get(User, actor_id)
            if actor is None:
                raise ValueError("审批操作人尚未映射到本地用户")

            comment = item.get("comment") or ""
            if not isinstance(comment, str) or len(comment) > 500:
                raise ValueError("审批意见格式错误")

            next_id = item.get("current_approver_id")
            if route:
                from app.services.business_rules.expense_adapter import validate_fixed_route
                await validate_fixed_route(db, expense, new_status, next_id)
            if new_status in POLL_STATUSES:
                await validate_actor(db, next_id, expense.user_id)
            elif next_id is not None:
                raise ValueError("终态不应保留待审批人")

            detail = {}
            if new_status == "paid":
                paid_at = parse_finance_time(item["paid_at"])
                amount = money(item["paid_amount"])
                if (
                    paid_at > occurred_at
                    or (
                        expense.submitted_at
                        and paid_at < expense.submitted_at
                    )
                ):
                    raise ValueError("打款时间异常")
                expense.paid_at = paid_at
                expense.paid_amount = amount
                detail = {
                    "paid_at": as_shanghai(paid_at).isoformat(),
                    "paid_amount": str(amount),
                }

            event = ExpenseApprovalLog(
                expense_id=expense.id,
                version=item["version"],
                from_status=expense.status,
                to_status=new_status,
                approver_id=actor_id,
                approver_name=actor.name,
                comment=comment,
                occurred_at=occurred_at,
                detail=detail,
                notified=False,
            )
            db.add(event)

            expense.status = new_status
            expense.approval_version = item["version"]
            expense.current_approver_id = next_id
            expense.node_started_at = occurred_at

            await enqueue_status(db, expense, event)

        if (
            expense.approval_version != snapshot["version"]
            or expense.status != snapshot.get("status")
            or expense.current_approver_id
            != snapshot.get("current_approver_id")
        ):
            raise ValueError("财务快照与事件历史不一致")

        expense.approval_synced_at = utc_now()


async def poll_all():
    last_id = 0
    while True:
        async with create_session() as db:
            ids = list((await db.scalars(
                select(Expense.id)
                .where(
                    Expense.id > last_id,
                    Expense.status.in_(POLL_STATUSES),
                    Expense.finance_no.is_not(None),
                )
                .order_by(Expense.id)
                .limit(100)
            )).all())

        if not ids:
            return

        for expense_id in ids:
            last_id = expense_id
            try:
                await sync_one(expense_id)
            except Exception as exc:
                log.error(
                    "expense_approval_sync_failed "
                    "expense_id=%s error_type=%s",
                    expense_id,
                    type(exc).__name__,
                )


async def approval_detail(user_id: int, expense_id: int):
    async with create_session() as db:
        expense = await owned_expense(db, user_id, expense_id)
        events = list((await db.scalars(
            select(ExpenseApprovalLog)
            .where(ExpenseApprovalLog.expense_id == expense.id)
            .order_by(ExpenseApprovalLog.version)
        )).all())

        return {
            "expense_id": expense.id,
            "expense_no": expense.expense_no,
            "total_amount": str(expense.total_amount),
            "current_status": expense.status,
            "status_label": STATUS_LABELS.get(
                expense.status, expense.status,
            ),
            "synced_at": (
                as_shanghai(expense.approval_synced_at).isoformat()
                if expense.approval_synced_at else None
            ),
            "approval_log": [{
                "from_status": event.from_status,
                "to_status": event.to_status,
                "approver_name": event.approver_name,
                "comment": event.comment,
                "occurred_at": as_shanghai(
                    event.occurred_at,
                ).isoformat(),
                "created_at": as_shanghai(
                    event.created_at,
                ).isoformat(),
            } for event in events],
        }


async def latest_expense_id(user_id: int):
    async with create_session() as db:
        return await db.scalar(
            select(Expense.id)
            .where(
                Expense.user_id == user_id,
                Expense.submitted_at.is_not(None),
            )
            .order_by(Expense.submitted_at.desc(), Expense.id.desc())
            .limit(1)
        )


async def scan_timeouts():
    now = utc_now()
    today = as_shanghai(now).date()
    grouped = defaultdict(list)

    async with create_session() as db:
        calendar_today = await load_calendar(db, today, today)
        if not calendar_today[today]:
            return

        expenses = list((await db.scalars(
            select(Expense)
            .where(
                Expense.status.in_(POLL_STATUSES),
                Expense.current_approver_id.is_not(None),
                Expense.node_started_at.is_not(None),
            )
            .order_by(Expense.id)
        )).all())

        for expense in expenses:
            # 财务查询持续失败时，不凭陈旧审批人信息发送催办。
            if (
                expense.approval_synced_at is None
                or now - expense.approval_synced_at
                > timedelta(minutes=15)
            ):
                continue

            start_date = as_shanghai(
                expense.node_started_at,
            ).date()
            try:
                calendar = await load_calendar(
                    db, start_date, today,
                )
            except CalendarUnavailable:
                log.error(
                    "expense_calendar_missing expense_id=%s",
                    expense.id,
                )
                continue

            seconds = working_seconds(
                expense.node_started_at, now, calendar,
            )
            if seconds <= 3 * 24 * 3600:
                continue

            approver = await db.get(
                User, expense.current_approver_id,
            )
            if (
                approver is None
                or approver.status not in ACTIVE_STATUSES
                or approver.user_id == expense.user_id
            ):
                continue

            grouped[approver.user_id].append({
                "expense_id": expense.id,
                "expense_no": expense.expense_no,
                "total_amount": str(expense.total_amount),
                "status": expense.status,
                "version": expense.approval_version,
                "working_days": round(seconds / 86400, 2),
            })

    for receiver_id, items in grouped.items():
        # 每位审批人每日只建立一组提醒。
        # 每组的全部分片在同一事务内创建，避免部分创建后重复。
        prefix = f"expense-timeout:{today}:{receiver_id}:"
        async with create_session() as db, db.begin():
            existing = await db.scalar(
                select(ExpenseNotification.id)
                .where(ExpenseNotification.event_key == prefix + "0")
            )
            if existing:
                continue

            for offset in range(0, len(items), 20):
                batch = items[offset:offset + 20]
                db.add(ExpenseNotification(
                    event_key=prefix + str(offset // 20),
                    receiver_id=receiver_id,
                    kind="timeout",
                    payload={
                        "day": today.isoformat(),
                        "items": batch,
                        "card": timeout_card(batch),
                    },
                    status="pending",
                    next_attempt_at=now,
                ))
