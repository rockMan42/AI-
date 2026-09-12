from dataclasses import dataclass


from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy import select

from app.models import User
from app.security.auth import ACTIVE_STATUSES

HR_ROLES = {"hr", "HR", "人事"}

@dataclass(frozen=True)
class AttendanceTarget:
    user_id: int
    name: str

async def authorize_attendance(
        db: AsyncSession,
        actor_open_id: str,
        target_user_id: int
) -> AttendanceTarget:

    """授权考勤查询
       先判断用户是否是目标用户，是则返回（当前用户就是目标用户）
       否则判断用户角色是否是HR，是则返回
       否则判断用户和目标用户是否在同一个部门，是则返回
       否则抛出权限错误（最后返回要查询的目标用户）
    """
    actor = await db.scalar(select(User).where(User.feishu_open_id == actor_open_id))

    if actor is None or actor.status not in ACTIVE_STATUSES:
        raise PermissionError("用户不存在或已禁用")

    if actor.user_id == target_user_id:
        return AttendanceTarget(user_id=actor.user_id, name=actor.name)

    if actor.role not in HR_ROLES:
        raise PermissionError("无权查询该员工的考勤")

    target_user = await db.scalar(select(User).where(User.user_id == target_user_id))

    if target_user is None or target_user.status not in ACTIVE_STATUSES or actor.department_id is None or target_user.department_id != actor.department_id:
        raise PermissionError("无权查询该员工的考勤")

    return AttendanceTarget(user_id=target_user.user_id, name=target_user.name)





