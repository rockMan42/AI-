from copy import deepcopy
from decimal import Decimal
from typing import Literal
from pydantic import Field
from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import create_session
from app.models import Expense, FinanceExpenseMock, User
from app.schemas.expense import StrictModel
from app.security.auth import ACTIVE_STATUSES, ADMIN_ROLES
from app.services.expense.approval_state import (
    POLL_STATUSES,
    REJECTED_STATUSES,
    ensure_transition,
    parse_finance_time,
)
from app.services.expense.rules import ExpenseError
from app.utils.time import as_utc, utc_now


class MockApprovalInput(StrictModel):
    request_id: str = Field(
        min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_-]+$",
    )
    to_status: Literal[
        "manager_approved",
        "manager_rejected",
        "finance_approved",
        "finance_rejected",
        "rejected",
        "paid",
    ]
    next_approver_id: int | None = Field(default=None, gt=0)
    comment: str = Field(default="", max_length=500)
    paid_amount: Decimal | None = Field(
        default=None, gt=0, max_digits=12, decimal_places=2,
    )
    paid_at: str | None = None


async def advance_mock(admin_id, expense_id, body: MockApprovalInput):
    if not get_settings().expense_mock_enabled:
        raise ExpenseError("模拟审批入口未开启", 404)

    async with create_session() as db, db.begin():
        admin = await db.get(User, admin_id)
        if (
            admin is None
            or admin.role not in ADMIN_ROLES
            or admin.status not in ACTIVE_STATUSES
        ):
            raise ExpenseError("仅管理员可模拟审批", 403)

        # 与 MCP 保持同样的加锁顺序：报销单 → 财务模拟记录。
        expense = await db.scalar(
            select(Expense)
            .where(Expense.id == expense_id)
            .with_for_update()
        )
        row = await db.scalar(
            select(FinanceExpenseMock)
            .where(FinanceExpenseMock.expense_id == expense_id)
            .with_for_update()
        )
        if expense is None or row is None or not row.approval_state:
            raise ExpenseError("财务审批记录不存在", 404)

        state = deepcopy(row.approval_state)
        command = body.model_dump(mode="json")

        for event in state["events"]:
            if event["request_id"] == body.request_id:
                if event["command"] != command:
                    raise ExpenseError("同一请求编号不能更换审批参数")
                return {
                    "status": row.status,
                    "version": state["version"],
                }

        from app.services.business_rules.expense_adapter import validate_fixed_route
        await validate_fixed_route(db, expense, current_status=row.status)
        await validate_fixed_route(db, expense, body.to_status, body.next_approver_id)
        route = (expense.payload or {}).get("approval_route")
        if route:
            stage_index = {"submitted": 0, "manager_approved": 1, "finance_approved": 2}.get(row.status)
            if stage_index is None or state["current_approver_id"] != route["nodes"][stage_index]["user_id"]:
                raise ExpenseError("当前审批人与固定路线不一致", 409)

        try:
            ensure_transition(row.status, body.to_status)
        except ValueError as exc:
            raise ExpenseError(str(exc)) from None

        if body.to_status in REJECTED_STATUSES and not body.comment.strip():
            raise ExpenseError("退回必须填写原因", 422)

        if body.to_status in POLL_STATUSES:
            next_user = (
                await db.get(User, body.next_approver_id)
                if body.next_approver_id else None
            )
            if (
                next_user is None
                or next_user.status not in ACTIVE_STATUSES
                or next_user.user_id == expense.user_id
            ):
                raise ExpenseError("下一节点审批人无效", 422)
        elif body.next_approver_id is not None:
            raise ExpenseError("终态不能指定下一审批人", 422)

        now = utc_now()
        paid_at = None
        paid_amount = None

        if body.to_status == "paid":
            if body.paid_amount is None or body.paid_at is None:
                raise ExpenseError("打款必须提供金额和实际时间", 422)
            try:
                parsed_paid_at = parse_finance_time(body.paid_at)
                submitted_at = parse_finance_time(state["submitted_at"])
            except ValueError:
                raise ExpenseError("打款时间格式无效", 422) from None

            if not submitted_at <= parsed_paid_at <= now:
                raise ExpenseError("打款时间不在有效范围内", 422)
            paid_at = as_utc(parsed_paid_at).isoformat()
            paid_amount = str(body.paid_amount)
        elif body.paid_at is not None or body.paid_amount is not None:
            raise ExpenseError("非打款事件不能填写打款信息", 422)

        # Mock 中模拟当前审批人完成节点，管理员只是测试操作者。
        actor_id = state["current_approver_id"]
        actor = await db.get(User, actor_id) if actor_id else None
        if actor is None:
            raise ExpenseError("当前审批人不存在")

        event = {
            "version": state["version"] + 1,
            "request_id": body.request_id,
            "command": command,
            "from_status": row.status,
            "to_status": body.to_status,
            "approver_id": actor_id,
            "comment": body.comment.strip(),
            "current_approver_id": body.next_approver_id,
            "occurred_at": as_utc(now).isoformat(),
            "paid_at": paid_at,
            "paid_amount": paid_amount,
            "mock_operator_id": admin_id,
        }
        state["events"].append(event)
        state["version"] = event["version"]
        state["current_approver_id"] = body.next_approver_id
        state["node_started_at"] = event["occurred_at"]
        state["paid_at"] = paid_at
        state["paid_amount"] = paid_amount

        row.status = body.to_status
        row.approval_state = state

        # 不修改 Expense.status，必须让真实轮询链路发现变化。
        return {
            "status": row.status,
            "version": state["version"],
        }
