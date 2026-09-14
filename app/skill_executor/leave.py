from pydantic import ValidationError
from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.leave import LeaveInput
from app.schemas.skill_context import SkillContext
from app.services.attendance.leave_cards import confirmation_card
from app.services.attendance.leave_service import LeaveError, LeaveService
from app.services.conversation_engine.session_store import SessionStore
from app.skill_executor.base import BaseSkillExecutor, SkillResult


LEAVE_TYPE_CODES = {
    "年假": "annual",
    "调休": "compensatory",
    "病假": "sick",
    "事假": "personal",
}


class LeaveSkillExecutor(BaseSkillExecutor):
    async def executor(
        self,
        context: SkillContext,
        slots: dict,
        db: AsyncSession,
    ) -> SkillResult:
        try:
            body = LeaveInput(
                leave_type=LEAVE_TYPE_CODES.get(slots.get("leave_type"), ""),
                start_date=slots.get("start_time"),
                end_date=slots.get("end_time"),
                reason=slots.get("reason"),
            )
            session = await SessionStore().load(context.session_id)
            if session is None:
                return SkillResult(False, "会话已过期，请重新申请")

            draft = await LeaveService().prepare(
                context.open_id,
                body,
                flow_key=str(session.intent_started_at),
            )
        except ValidationError:
            return SkillResult(False, "请检查请假类型、日期及必填理由")
        except (LeaveError, ValueError) as exc:
            return SkillResult(False, str(exc))

        return SkillResult(
            success=True,
            message="请确认请假信息，点击确认后才会提交。",
            data={"awaiting_confirmation": True},
            card=confirmation_card(draft["draft_id"], draft),
        )