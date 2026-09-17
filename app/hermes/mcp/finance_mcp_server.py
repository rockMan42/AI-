import logging
import sys
from contextlib import asynccontextmanager
from uuid import uuid4

from mcp.server import FastMCP
from mcp.server.fastmcp.exceptions import ToolError
from sqlalchemy import select
from app.config.settings import get_settings
from app.core.database import init_db, close_db, create_session
from app.models import User, Expense, FinanceExpenseMock
from app.security.auth import ACTIVE_STATUSES
from app.security.expense import verify_finance_token
from app.services.expense.rules import ExpenseError, digest


@asynccontextmanager
async def lifespan(server):
    await init_db(get_settings())
    try:
        yield {}
    finally:
        await close_db()

mcp_server = FastMCP(
    name="finance_expense_mcp",
    lifespan=lifespan,
)

def receipt_data(row):
    return {
        "expense_id": row.expense_id,
        "finance_no": row.finance_no,
        "status": row.status,
        "payload_hash": row.payload_hash,
    }

async def authorize(db, identity_token, expense_id):
    user_id = verify_finance_token(identity_token, expense_id)
    user = await db.get(User, user_id)
    if user is None or user.status not in ACTIVE_STATUSES:
        raise ExpenseError("提交人不存在或已停用", 403)


    expense = await db.scalar(
        select(Expense)
        .where(Expense.id == expense_id)
        .with_for_update()
    )
    if expense is None or expense.user_id != user_id:
        raise ExpenseError("无权操作该报销单", 403)
    return expense



@mcp_server.tool(name="submit_expense")
async def submit_expense(
    expense_id: int,
    identity_token: str,
) -> dict:
    """模拟财务系统接收报销单，按 expense_id 持久化幂等。"""
    try:
        async with create_session() as db, db.begin():
            expense = await authorize(db, identity_token, expense_id)

            if expense.status not in {
                "submitting", "submitted", "approved", "paid",
            }:
                raise ExpenseError("报销单尚未由员工确认提交")

            if not expense.payload:
                raise ExpenseError("缺少报销提交快照")

            payload = {
                "expense_no": expense.expense_no,
                "user_id": expense.user_id,
                "trip_id": expense.trip_id,
                "total_amount": str(expense.total_amount),
                "has_override": bool(expense.has_override),
                "override_reason": expense.override_reason,
                "approver_id": expense.approver_id,
                "detail": expense.payload,
            }
            payload_hash = digest(payload)

            existing = await db.scalar(
                select(FinanceExpenseMock).where(
                    FinanceExpenseMock.expense_id == expense_id,
                )
            )
            if existing:
                if existing.payload_hash != payload_hash:
                    raise ExpenseError("相同报销单的提交内容发生变化")
                return receipt_data(existing)

            row = FinanceExpenseMock(
                expense_id=expense_id,
                finance_no="FIN-" + uuid4().hex.upper(),
                payload_hash=payload_hash,
                payload=payload,
                status=(
                    "pending_manager"
                    if expense.has_override
                    else "pending_finance"
                ),
            )
            db.add(row)
            await db.flush()
            return receipt_data(row)

    except ExpenseError as exc:
        raise ToolError(str(exc)) from None


@mcp_server.tool(name="query_expense")
async def query_expense(
    expense_id: int,
    identity_token: str,
) -> dict:
    """按业务报销单ID查询模拟财务接收结果。"""
    try:
        async with create_session() as db, db.begin():
            await authorize(db, identity_token, expense_id)
            row = await db.scalar(
                select(FinanceExpenseMock).where(
                    FinanceExpenseMock.expense_id == expense_id,
                )
            )
            if row is None:
                return {
                    "expense_id": expense_id,
                    "found": False,
                }
            return {"found": True, **receipt_data(row)}
    except ExpenseError as exc:
        raise ToolError(str(exc)) from None



if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        stream=sys.stderr,
    )
    mcp_server.run(transport="stdio")