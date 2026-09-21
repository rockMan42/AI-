from datetime import UTC, datetime, timedelta

import jwt
from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.requisition import Requisition
from app.models.user import User
from app.schemas.permission import AccessDenied
from app.security.auth import ACTIVE_STATUSES
from app.services.permission_audit import record_audit


AUDIENCE = "requisition-mcp"


def signing_key() -> str:
    key = get_settings().auth_jwt_secret.get_secret_value()
    if len(key) < 32:
        raise RuntimeError("内部凭证密钥未配置")
    return key


def issue_requisition_token(
    user_id: int,
    tool: str,
    resource_id: int | None,
    *,
    system: bool = False,
) -> str:
    if tool not in {"submit_requisition", "query_requisition"}:
        raise ValueError("不支持的物资工具")
    if system and tool != "query_requisition":
        raise ValueError("系统凭证仅允许查询")

    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "tool": tool,
            "resource_id": resource_id,
            "system": system,
            "iss": get_settings().auth_jwt_issuer,
            "aud": AUDIENCE,
            "iat": now,
            "exp": now + timedelta(minutes=2),
        },
        signing_key(),
        algorithm="HS256",
    )


async def authorize_requisition(
    token: str,
    tool: str,
    resource_id: int | None = None,
    applicant_id: int | None = None,
):
    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=["HS256"],
            issuer=get_settings().auth_jwt_issuer,
            audience=AUDIENCE,
            options={"require": [
                "sub", "tool", "resource_id", "system", "iat", "exp",
            ]},
        )

        if claims["tool"] != tool or claims["resource_id"] != resource_id:
            raise ValueError

        user_id = int(claims["sub"])
        system = claims["system"]

        if user_id <= 0 or type(system) is not bool:
            raise ValueError
        if system and tool != "query_requisition":
            raise ValueError
    except (jwt.PyJWTError, TypeError, ValueError):
        raise AccessDenied("物资调用凭证无效") from None

    async with create_session() as db:
        user = await db.get(User, user_id)
        allowed = user is not None and (
            system or user.status in ACTIVE_STATUSES
        )

        if tool == "submit_requisition":
            allowed = allowed and applicant_id == user_id
        else:
            owner = await db.scalar(
                select(Requisition.user_id)
                .where(Requisition.id == resource_id)
            )
            allowed = allowed and owner == user_id

    await record_audit(
        user_id,
        f"requisition.{tool}",
        allowed,
        resource_id,
        {"system": system},
    )

    if not allowed:
        raise AccessDenied("无权操作该申领单")