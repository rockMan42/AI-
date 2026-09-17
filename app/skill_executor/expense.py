from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.skill_context import SkillContext
from app.skill_executor.base import BaseSkillExecutor, SkillResult


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