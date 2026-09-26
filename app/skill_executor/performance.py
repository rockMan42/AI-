from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.skill_context import SkillContext
from app.schemas.skill_result import SkillResult
from app.schemas.permission import Role
from app.services.performance import PerformanceError, PerformanceService
from app.services.performance_cards import performance_card
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
        cycle = str(slots.get("period") or "").strip()
        if not cycle:
            return SkillResult(False, "请提供绩效周期。")

        service = PerformanceService()
        grade = slots.get("grade")
        try:
            if principal.role == Role.EMPLOYEE:
                data = await service.personal(db, principal, cycle)
                title = "我的绩效"
            elif principal.role == Role.MANAGER:
                data = await service.team(db, principal, cycle, grade)
                title = "团队绩效汇总"
            else:
                data = await service.overview(
                    db, principal, cycle, grade,
                )
                title = "公司绩效概览"
        except PerformanceError as exc:
            return SkillResult(False, str(exc))

        return SkillResult(
            True,
            title,
            data=data,
            card=performance_card(title, data),
        )
