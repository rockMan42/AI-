from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.models.user import User
from app.schemas.permission import AccessDenied
from app.security.auth import ACTIVE_STATUSES
from app.security.permission import authorize
from app.services.role_mapper import resolve_principal


@dataclass(frozen=True)
class AttendanceTarget:
    user_id: int
    name: str


async def authorize_attendance(
    db: AsyncSession,
    actor_open_id: str,
    target_user_id: int,
) -> AttendanceTarget:
    principal = await resolve_principal(actor_open_id)

    await authorize(
        principal,
        "attendance.read",
        target_user_id=target_user_id,
    )

    target = await db.scalar(
        select(User).where(
            User.user_id == target_user_id,
            User.status.in_(ACTIVE_STATUSES),
        )
    )
    if target is None:
        raise AccessDenied("目标用户不存在或已停用")

    return AttendanceTarget(target.user_id, target.name)