"""为本地接口调试创建指定用户的短期登录会话。"""

import argparse
import asyncio
import secrets
import time
from uuid import uuid4

from app.config.settings import get_settings
from app.core.database import close_db, create_session, init_db
from app.models.permission import AuthSession
from app.models.user import User
from app.schemas.permission import Principal, Role
from app.security.auth import ACTIVE_STATUSES
from app.services.auth import access_token, digest, signing_key


USER_ROLES = {
    "admin": Role.HR_ADMIN,
    "管理员": Role.HR_ADMIN,
    "manager": Role.MANAGER,
    "经理": Role.MANAGER,
    "employee": Role.EMPLOYEE,
    "员工": Role.EMPLOYEE,
}


def principal_from_user(user: User) -> Principal:
    role = USER_ROLES.get(user.role)
    if role is None:
        raise ValueError(f"不支持的用户角色：{user.role}")
    return Principal(
        user_id=user.user_id,
        open_id=user.feishu_open_id,
        role=role,
        department_id=user.department_id,
        version=0,
    )


async def issue_token(user_id: int) -> str:
    settings = get_settings()
    signing_key()
    await init_db(settings)
    try:
        async with create_session() as db:
            user = await db.get(User, user_id)
            if user is None or user.status not in ACTIVE_STATUSES:
                raise ValueError("用户不存在或已停用")
            principal = principal_from_user(user)

        session_id = uuid4().hex
        async with create_session() as db, db.begin():
            db.add(AuthSession(
                id=session_id,
                user_id=user_id,
                refresh_hash=digest(secrets.token_urlsafe(32)),
                expires_at=int(time.time()) + settings.auth_access_seconds,
                revoked=False,
            ))

        return access_token(principal, session_id)
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--user-id", type=int, required=True)
    parser.add_argument("--write-session", action="store_true", help="确认写入 auth_session")
    args = parser.parse_args()
    if not args.write_session:
        parser.error("必须添加 --write-session 才会创建登录会话")
    print(asyncio.run(issue_token(args.user_id)))
