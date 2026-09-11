from datetime import datetime
from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.leave_request import LeaveRequest
from app.schemas.scheme.skill_context import SkillContext
from app.skill_executor.base import BaseSkillExecutor, SkillResult


LEAVE_TYPE_CODES = {
    "年假": "annual",
    "病假": "sick",
    "事假": "personal",
    "婚假": "marriage",
    "产假": "maternity",
    "调休": "compensatory",
}


class LeaveSkillExecutor(BaseSkillExecutor):

    async def executor(self,context: SkillContext, slots: dict, db: AsyncSession) -> SkillResult:
        leave_type = slots.get("leave_type")
        start_time = self._parse_datetime(slots.get("start_time"))
        end_time = self._parse_datetime(slots.get("end_time"))

        if leave_type not in LEAVE_TYPE_CODES.keys():
            return SkillResult(success=False, message="请假类型无效，请重新选择。")
        if start_time is None or end_time is None:
            return SkillResult(success=False, message="请假开始时间和结束时间不能为空。")
        if end_time <= start_time:
            return SkillResult(success=False, message="请假结束时间必须晚于开始时间。")

        duration = slots.get("duration")
        if duration is None:
            duration = Decimal((end_time.date() - start_time.date()).days + 1)
        else:
            duration = Decimal(str(duration))

        leave_type_code = LEAVE_TYPE_CODES[leave_type]
        existing_result = await db.execute(
            select(LeaveRequest).where(
                LeaveRequest.user_id == context.user_id,
                LeaveRequest.leave_type == leave_type_code,
                LeaveRequest.start_time == start_time,
                LeaveRequest.end_time == end_time,
                LeaveRequest.status.in_(["pending", "approved"]),
            )
        )
        existing_request = existing_result.scalars().first()
        if existing_request is not None:
            return SkillResult(
                success=True,
                message=(
                    "相同时间段的请假申请已经存在，"
                    f"申请编号：{existing_request.id}"
                ),
                data={
                    "request_id": existing_request.id,
                    "duplicate": True,
                    "status": existing_request.status,
                },
            )

        leave_request = LeaveRequest(
            user_id=context.user_id,
            leave_type=leave_type_code,
            start_time=start_time,
            end_time=end_time,
            duration=duration,
            reason=slots.get("reason"),
        )

        try:
            db.add(leave_request)
            await db.commit()
            await db.refresh(leave_request)
        except Exception:
            await db.rollback()
            raise

        return SkillResult(
            success=True,
            message=f"请假申请已提交，申请编号：{leave_request.id}",
            data={
                "request_id": leave_request.id,
                "leave_type": leave_type,
                "start_time": start_time.isoformat(),
                "end_time": end_time.isoformat(),
                "duration": float(duration),
                "status": leave_request.status,
            },
        )

    @staticmethod
    def _parse_datetime(value) -> datetime | None:
        if isinstance(value, datetime):
            return value
        if value is None:
            return None
        return datetime.fromisoformat(str(value).replace("Z", "+00:00"))
