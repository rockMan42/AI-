from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.skill_context import SkillContext
from app.schemas.skill_result import SkillResult
from app.security.permission import authorize
from app.services.role_mapper import resolve_principal
from app.skill_executor.base import BaseSkillExecutor


class PerformanceSkillExecutor(BaseSkillExecutor):
    async def executor(
        self,
        context: SkillContext,
        slots: dict,
        db: AsyncSession,
    ) -> SkillResult:
        principal = await resolve_principal(context.open_id)
        await authorize(principal, "performance.read")

        return SkillResult(False, "绩效查询尚未接入数据源。")