import hashlib
import hmac
import secrets
import time
from urllib.parse import urlencode
from uuid import uuid4

import jwt
from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.core import redis_client as redis_module
from app.core.database import create_session
from app.models.permission import AuthSession
from app.models.user import User
from app.schemas.permission import PermissionUnavailable, Principal, Role
from app.services.conversation_engine.feishu import (
    FEISHU_API_BASE,
    get_feishu_client,
)
from app.services.role_mapper import resolve_principal
from app.services.user import get_or_create_user


def digest(value: str) -> str:
    return hashlib.sha256(value.encode()).hexdigest()


def signing_key() -> str:
    key = get_settings().auth_jwt_secret.get_secret_value()
    if len(key) < 32:
        raise PermissionUnavailable("认证密钥未配置")
    return key


def access_token(principal: Principal, session_id: str) -> str:
    settings = get_settings()
    now = int(time.time())

    return jwt.encode(
        {
            "sub": principal.open_id,
            "sid": session_id,
            "user_id": principal.user_id,
            "role": principal.role.value,
            "department_id": principal.department_id,
            "iss": settings.auth_jwt_issuer,
            "aud": settings.auth_jwt_audience,
            "iat": now,
            "exp": now + settings.auth_access_seconds,
        },
        signing_key(),
        algorithm="HS256",
    )


def decode_access(token: str) -> dict:
    settings = get_settings()

    try:
        claims = jwt.decode(
            token,
            signing_key(),
            algorithms=["HS256"],
            issuer=settings.auth_jwt_issuer,
            audience=settings.auth_jwt_audience,
            options={"require": [
                "sub", "sid", "user_id", "role", "department_id",
                "iat", "exp", "iss", "aud",
            ]},
        )

        if (
            not isinstance(claims["sub"], str)
            or not claims["sub"]
            or not isinstance(claims["sid"], str)
            or len(claims["sid"]) != 32
            or type(claims["user_id"]) is not int
            or claims["user_id"] <= 0
            or type(claims["department_id"]) is not int
            or type(claims["iat"]) is not int
            or type(claims["exp"]) is not int
            or not 0 < claims["exp"] - claims["iat"] <= 7200
        ):
            raise ValueError

        Role(claims["role"])
        return claims
    except (jwt.PyJWTError, TypeError, ValueError):
        raise HTTPException(
            401,
            "身份凭证无效或已过期",
            headers={"WWW-Authenticate": "Bearer"},
        ) from None


def token_response(principal: Principal, session_id: str, secret: str) -> dict:
    return {
        "access_token": access_token(principal, session_id),
        "refresh_token": f"{session_id}.{secret}",
        "token_type": "Bearer",
        "expires_in": get_settings().auth_access_seconds,
    }


async def begin_login() -> tuple[str, str]:
    settings = get_settings()
    if not settings.auth_callback_url:
        raise PermissionUnavailable("OAuth 回调地址未配置")

    state = secrets.token_urlsafe(32)
    await redis_module.redis_client.set(
        f"dep:oauth:state:{digest(state)}",
        "1",
        ex=300,
        nx=True,
    )

    query = urlencode({
        "client_id": settings.feishu_app_id,
        "response_type": "code",
        "redirect_uri": settings.auth_callback_url,
        "scope": "contact:user.base:readonly",
        "state": state,
    })

    return f"https://accounts.feishu.cn/open-apis/authen/v1/authorize?{query}", state


async def complete_login(code: str, state: str, cookie_state: str) -> dict:
    if (
        not state
        or not cookie_state
        or not hmac.compare_digest(state, cookie_state)
    ):
        raise HTTPException(401, "登录状态校验失败")

    saved = await redis_module.redis_client.getdel(
        f"dep:oauth:state:{digest(state)}"
    )
    if saved is None:
        raise HTTPException(401, "登录状态已过期或已使用")

    settings = get_settings()
    client = get_feishu_client()

    response = await client.post(
        f"{FEISHU_API_BASE}/authen/v2/oauth/token",
        json={
            "grant_type": "authorization_code",
            "client_id": settings.feishu_app_id,
            "client_secret": settings.feishu_app_secret,
            "code": code,
            "redirect_uri": settings.auth_callback_url,
        },
    )
    response.raise_for_status()
    payload = response.json()

    feishu_token = payload.get("access_token")
    if payload.get("code", 0) != 0 or not feishu_token:
        raise HTTPException(401, "飞书登录失败")

    response = await client.get(
        f"{FEISHU_API_BASE}/authen/v1/user_info",
        headers={"Authorization": f"Bearer {feishu_token}"},
    )
    response.raise_for_status()
    payload = response.json()

    open_id = (payload.get("data") or {}).get("open_id")
    if payload.get("code") != 0 or not open_id:
        raise HTTPException(401, "无法获取飞书身份")

    async with create_session() as db:
        user = await get_or_create_user(open_id, db)

    principal = await resolve_principal(user.feishu_open_id)
    session_id = uuid4().hex
    secret = secrets.token_urlsafe(32)

    async with create_session() as db, db.begin():
        db.add(AuthSession(
            id=session_id,
            user_id=principal.user_id,
            refresh_hash=digest(secret),
            expires_at=int(time.time()) + settings.auth_refresh_seconds,
            revoked=False,
        ))

    return token_response(principal, session_id, secret)


async def refresh_login(refresh_token: str) -> dict:
    try:
        session_id, secret = refresh_token.split(".", 1)
        if len(session_id) != 32 or not secret:
            raise ValueError
    except ValueError:
        raise HTTPException(401, "刷新凭证无效") from None

    response = None

    async with create_session() as db, db.begin():
        session = await db.scalar(
            select(AuthSession)
            .where(AuthSession.id == session_id)
            .with_for_update()
        )

        if (
            session is None
            or session.revoked
            or session.expires_at <= time.time()
        ):
            raise HTTPException(401, "登录会话已过期")

        if not hmac.compare_digest(session.refresh_hash, digest(secret)):
            # 已使用的刷新凭证被再次提交：撤销整个会话。
            session.revoked = True
        else:
            user = await db.get(User, session.user_id)
            if user is None:
                session.revoked = True
            else:
                principal = await resolve_principal(user.feishu_open_id)
                next_secret = secrets.token_urlsafe(32)
                session.refresh_hash = digest(next_secret)
                response = token_response(principal, session.id, next_secret)

    if response is None:
        raise HTTPException(401, "刷新凭证失效，请重新登录")

    return response