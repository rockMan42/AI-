from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.rag_client import RAGError
from app.hermes.agent import call_knowledge_search
from app.models import User
from app.schemas.scheme.knowledge import KnowledgeSearchRequest

from app.schemas.scheme.skill_context import SkillContext
from app.security.auth import ACTIVE_STATUSES, accessible_permission_level
from app.skill_executor.base import BaseSkillExecutor, SkillResult


class PolicySkillExecutor(BaseSkillExecutor):

    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        # 校验用户
        user = await db.get(User, int(context.user_id))

        if user is None or user.status not in ACTIVE_STATUSES:
            return SkillResult(
                success=False,
                message="用户不存在或者已停用"
            )

        try:
            permissions = await accessible_permission_level(user)

            request = KnowledgeSearchRequest(
                query=slots.get("query_topic",""),
                permission_level=permissions[-1]
            )

            response = await call_knowledge_search(request, user)

            return SkillResult(
                success=True,
                message=response.answer,
                data=response.model_dump(mode="json")
            )



        except ValidationError:
            return SkillResult(
                success=False,
                message="请提供1～1024字的完整知识查询问题。"
            )
        except(PermissionError,RAGError) as exc:
            return SkillResult(
                success=False,
                message=str(exc)
            )
