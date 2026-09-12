from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.skill_context import SkillContext
from app.skill_executor.base import BaseSkillExecutor, SkillResult


class RequisitionSkillExecutor(BaseSkillExecutor):

    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        return SkillResult(
            success=True,
            message="物资申请成功"
        )