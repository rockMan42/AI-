from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.skill_context import SkillContext
from app.skill_executor.base import BaseSkillExecutor, SkillResult
from app.services.expense.approval_cards import query_card
from app.services.expense.approval_service import (
    approval_detail,
    latest_expense_id,
)
from app.services.expense.rules import ExpenseError


class ExpenseStatusQueryExecutor(BaseSkillExecutor):
    async def executor(
        self,
        context: SkillContext,
        slots: dict,
        db: AsyncSession,
    ) -> SkillResult:
        try:
            user_id = int(context.user_id)
            expense_id = slots.get("expense_id")
            if expense_id is None:
                expense_id = await latest_expense_id(user_id)

            if expense_id is None:
                return SkillResult(
                    success=True,
                    message="暂无已提交的报销单。",
                )

            if isinstance(expense_id, bool):
                raise ValueError
            expense_id = int(expense_id)
            if expense_id <= 0:
                raise ValueError

            data = await approval_detail(user_id, expense_id)
            return SkillResult(
                success=True,
                message=data["status_label"],
                data=data,
                card=query_card(data),
            )
        except ExpenseError as exc:
            return SkillResult(success=False, message=str(exc))
        except (TypeError, ValueError):
            return SkillResult(
                success=False,
                message="报销单编号无效，请提供正整数编号。",
            )

class ExpenseSkillExecutor(BaseSkillExecutor):

    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        return SkillResult(
            success=True,
            message=(
                "请直接发送发票图片，支持连续发送多张。"
                "识别完成后，请逐张确认或修改。"
            ),
            data={"awaiting_upload": True}
        )