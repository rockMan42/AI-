from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import LeaveBalance


def leave_balance_statement(
        user_id: int,
        year: int,
        leave_types: tuple[str,...]
):
    return (select(LeaveBalance).where(LeaveBalance.user_id == user_id,LeaveBalance.year == year,LeaveBalance.leave_type.in_(leave_types)))

async def load_leave_balances(
        db: AsyncSession,
        user_id: int,
        year: int,
        leave_types: tuple[str,...],
        for_update: bool = False
) -> dict[str,LeaveBalance]:
    statement = leave_balance_statement(user_id, year, leave_types)
    if for_update:
        statement = statement.with_for_update()

    rows = (await db.scalars(statement)).all()

    return { row.leave_type: row for row in rows}


