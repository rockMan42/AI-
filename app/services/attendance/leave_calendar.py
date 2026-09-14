from datetime import date, datetime, timedelta, time
from decimal import Decimal
from unittest import result

from sqlalchemy.ext.asyncio import AsyncSession

from app.models import WorkCalendarDay

from sqlalchemy import select

from app.utils.time import SHANGHAI_TIMEZONE, as_shanghai



class CalendarUnavailable(ValueError):
    pass

async def load_calendar(
        db: AsyncSession,
        start: date,
        end: date
) -> dict[date,bool]:


    rows = (
        await db.scalars(select(WorkCalendarDay).where(WorkCalendarDay.day.between(start, end)))
    ).all()

    calendar = {row.day: row.is_workday for row in rows}
    expected = (end - start).days + 1

    if len(calendar) != expected:
        raise CalendarUnavailable("工作日历不完整，请联系管理员")

    return calendar

async def calculate_year_days(
        db: AsyncSession,
        start: date,
        end: date
) -> dict[str, str]:
    calendar = await load_calendar(db,start,end)
    result: dict[str,Decimal] = {}

    for day, is_workday in calendar.items():
        if is_workday:
            year = str(day.year)
            result[year] = result.get(year, Decimal("0")) + Decimal("1")

    if not result:
        raise ValueError("所选日期没有工作日，无需申请请假")

    return {
        year: str(result[year])
        for year in sorted(result)
    }


async def is_overdue(
    db: AsyncSession,
    created_at: datetime,
    now: datetime,
) -> bool:
    """
    工作日历是业务配置数据，需要导入公司认可的完整日历。不要只插入节假日，否则无法区分“普通工作日”和“漏配日期”。
    :param db:
    :param created_at:
    :param now:
    :return:
    """
    start = as_shanghai(created_at)
    end = as_shanghai(now)

    if end <= start:
        return False

    calendar = await load_calendar(db, start.date(), end.date())

    # 即使已累计超时，也不在休息日发送升级提醒。
    if not calendar[end.date()]:
        return False

    seconds = 0.0
    for day, is_workday in calendar.items():
        if not is_workday:
            continue

        day_start = datetime.combine(day, time.min, SHANGHAI_TIMEZONE)
        day_end = day_start + timedelta(days=1)
        left = max(start, day_start)
        right = min(end, day_end)
        if right > left:
            seconds += (right - left).total_seconds()

    return seconds >= 48 * 3600