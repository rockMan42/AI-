import logging
from calendar import calendar
from datetime import date

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession
import calendar

from app.models.attendance import Attendance
from app.skill_executor.base import BaseSkillExecutor, SkillResult
from app.models.scheme.skill_context import SkillContext

log = logging.getLogger(__name__)

class AttendanceSKillExecutor(BaseSkillExecutor):

    async def executor(self, context: SkillContext, slots: dict,  db: AsyncSession) -> SkillResult:
        conditions = []

        query_date = slots.get("query_date", None)
        query_month = slots.get("query_month",None)
        status_filter = slots.get("status_filter",None)
        user_id = context.user_id

        log.info(f"查询日期:{query_date},查询月份:{query_month},状态过滤:{status_filter},用户ID:{user_id}")
        # 构建查询条件
        conditions.append(Attendance.user_id == user_id)

        if query_date:
            conditions.append(Attendance.date == query_date)
        if query_month:
            year, month = map(int,query_month.split("-"))
            start_date = date(year, month, 1)
            last_date = date(year,month,calendar.monthrange(year,month)[1])
            conditions.append(Attendance.date.between(start_date, last_date))
        if status_filter:
            conditions.append(Attendance.status == status_filter)
        if user_id:
            conditions.append(Attendance.user_id == user_id)

        result = await db.execute(select(Attendance).where(*conditions))
        log.info(f"构造条件:{conditions}")

        attendances = result.scalars().all()

        # attendances 是列表，没有 __dict__
        data = [
            {
                "date": item.date.isoformat(),
                "clock_in_time": item.clock_in_time.isoformat() if item.clock_in_time else None,
                "clock_out_time": item.clock_out_time.isoformat() if item.clock_out_time else None,
                "status": item.status,
                "work_hours": float(item.work_hours) if item.work_hours else None,
                "remark": item.remark,
            }
            for item in attendances
        ]
        log.info(f"查询结果:{data}")

        if not data:
            print("没有找到考勤记录")
            return SkillResult(success=False, message="没有找到考勤记录")

        return SkillResult(success=True, message="找到了您的考勤记录",data=data)