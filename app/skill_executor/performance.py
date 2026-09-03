from sqlalchemy.ext.asyncio import AsyncSession

from app.models.scheme.skill_context import SkillContext
from app.skill_executor.base import BaseSkillExecutor, SkillResult


class PerformanceSkillExecutor(BaseSkillExecutor):

    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        # 绩效权限校验
        if context.role and context.role not  in ["admin","manager"]:
            return SkillResult(
                success=False,
                message="该功能仅限主管及以上角色使用"
            )

