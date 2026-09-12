import asyncio
import json

from sqlalchemy.ext.asyncio import AsyncSession
from tools.registry import registry

from app.schemas.skill_context import SkillContext
from app.schemas.skill_result import SkillResult
from app.skill_executor.base import BaseSkillExecutor


class AttendanceSKillExecutor(BaseSkillExecutor):
    async def executor(
        self,
        context: SkillContext,
        slots: dict,
        db: AsyncSession,
    ) -> SkillResult:
        if (
            slots.get("query_target") == "other"
            and not slots.get("target_user_id")
        ):
            return SkillResult(
                success=False,
                message="查询其他员工时，请提供其系统用户 ID。",
            )

        target_id = slots.get("target_user_id") or context.user_id
        
        arguments = {
            "user_id": str(target_id),
            "query_type": slots.get("query_type") or "all",
            "month": slots.get("query_month"),
            "query_date": slots.get("query_date"),
            "year": slots.get("query_year"),
            "status_filter": slots.get("status_filter"),
        }

        # dispatch 是同步入口，不能直接阻塞应用事件循环。
        from app.hermes.tools.attendance_tool import TOOL_NAME
        raw = await asyncio.to_thread(
            registry.dispatch,
            TOOL_NAME,
            arguments,
            actor_open_id=context.open_id,
        )

        payload = json.loads(raw) if isinstance(raw, str) else raw

        if not isinstance(payload, dict):
            return SkillResult(False, "考勤服务返回格式异常")

        if not payload.get("success"):
            return SkillResult(
                success=False,
                message=payload.get("message", "考勤查询失败"),
            )

        return SkillResult(
            success=True,
            message=payload["message"],
            data=payload["data"],
            card=payload["card"],
        )