from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheme.skill_context import SkillContext
from app.skill_executor.base import BaseSkillExecutor, SkillResult


class LeaveSkillExecutor(BaseSkillExecutor):

    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        pass