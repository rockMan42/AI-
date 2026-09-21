from typing import Annotated

import jwt
from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings, get_settings
from app.core.database import get_session
from app.models.user import User


bearer_scheme = HTTPBearer(auto_error=False)




ADMIN_ROLES = {"admin","管理员"}
ACTIVE_STATUSES = {"active", "活跃", "活动"}
INTERNAL_ROLES = ADMIN_ROLES | {"manager","经理","employee","员工"}

MANAGEMENT_ROLES = ADMIN_ROLES | {"manager", "经理"}

PERMISSION_SCOPES = {
    "public": ("public",),
    "internal": ("public", "internal"),
    "confidential": ("public", "internal", "confidential"),
}


async def get_current_user(
    credentials: Annotated[
        HTTPAuthorizationCredentials | None,
        Depends(bearer_scheme),
    ],
    db: Annotated[AsyncSession, Depends(get_session)],
) -> User:
    import time

    from app.models.permission import AuthSession
    from app.services.auth import decode_access

    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            401,
            "缺少身份凭证",
            headers={"WWW-Authenticate": "Bearer"},
        )

    claims = decode_access(credentials.credentials)
    session = await db.get(AuthSession, claims["sid"])

    if (
        session is None
        or session.revoked
        or session.expires_at <= time.time()
        or session.user_id != claims["user_id"]
    ):
        raise HTTPException(401, "登录会话已失效")

    user = await db.get(User, claims["user_id"])
    if (
        user is None
        or user.feishu_open_id != claims["sub"]
        or user.status not in ACTIVE_STATUSES
    ):
        raise HTTPException(403, "用户不存在或已停用")

    return user

async def require_admin(user: Annotated[
    User,Depends(get_current_user)
]) -> User:

    if user.role not in ADMIN_ROLES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可执行操作"
        )
    return user

async def accessible_permission_level(user: User) -> tuple[str,...]:
    if user.role in MANAGEMENT_ROLES:
        return PERMISSION_SCOPES["confidential"]

    if user.role in INTERNAL_ROLES:
        return PERMISSION_SCOPES["internal"]

    return PERMISSION_SCOPES["public"]


async def search_permission_levels(
        user: User,
        permission_level: str | None
) -> tuple[str,...]:

    if user.status not in ACTIVE_STATUSES:
        raise PermissionError("用户已停用")

    allowed = await accessible_permission_level(user)

    if permission_level is None:
        return allowed

    if permission_level not in PERMISSION_SCOPES:
        raise PermissionError("无效的权限级别")

    if permission_level not in allowed:
        raise PermissionError("用户无权限访问")

    requested = PERMISSION_SCOPES[permission_level]

    return tuple(level for level in requested if level in allowed)
