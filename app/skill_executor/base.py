from abc import ABC, abstractmethod
from sqlalchemy.ext.asyncio import AsyncSession
from app.models.scheme.skill_context import SkillContext
from app.models.scheme.skill_result import SkillResult

# 使用策略模式定义统一的执行接口
class BaseSkillExecutor(ABC):

    @abstractmethod
    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        pass
