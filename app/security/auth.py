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

ADMIN_ROlES = {"admin","管理员"}
ACTIVE_STATUSES = {"active", "活跃", "活动"}
INTERNAL_ROLES = ADMIN_ROlES | {"manager","经理","employee","员工"}


async def get_current_user(
        credentials: Annotated[
            HTTPAuthorizationCredentials | None,
            Depends(bearer_scheme)
        ],
        db: Annotated[AsyncSession,Depends(get_session)],
        settings: Annotated[
            Settings,
            Depends(get_settings)
        ]
) -> User:

    # 身份校验
    if credentials is None or credentials.scheme.lower() != "bearer":
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="缺少身份校验",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # jwt配置校验
    secret = settings.auth_jwt_secret.get_secret_value()
    if len(secret) < 32:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="身份认证服务未正确配置"
        )

    try:
        payload = jwt.decode(
            credentials.credentials,
            secret,
            algorithms=["HS256"],
            audience=settings.auth_jwt_audience,
            issuer=settings.auth_jwt_issuer,
            options={"require": ["sub", "iat", "exp"]},
        )
        open_id = payload["sub"]
        if not isinstance(open_id, str) or not open_id:
            raise jwt.InvalidTokenError("invalid sub")
    except jwt.PyJWTError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="身份凭证无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    smtp = select(User).where(User.feishu_open_id == open_id)
    result = await db.execute(smtp)
    user = result.scalar_one_or_none()

    if user is None or user.status not in ACTIVE_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="用户不存在或者已停用"
        )

    return user

async def require_admin(user: Annotated[
    User,Depends(get_current_user)
]) -> User:

    if user.role not in ADMIN_ROlES:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="仅管理员可执行操作"
        )
    return user

async def accessible_permission_level(user: User) -> tuple[str,...]:
    if user.role in ADMIN_ROlES:
        return "public","internal","confidential"

    if user.role in INTERNAL_ROLES:
        return "public","internal"

    return ("public",)
