import hashlib
import hmac
import re
import time

import jwt
from cryptography.fernet import Fernet
from sqlalchemy.ext.asyncio import AsyncSession

from app.config.settings import Settings
from app.core.rag_context import current_budget
from app.models import User
from app.security.auth import ACTIVE_STATUSES
from sqlalchemy import select
MCP_AUDIENCE = "enterprise-knowledge-mcp"

def _signing_key(settings: Settings) -> str:
    key = settings.auth_jwt_secret.get_secret_value()

    if len(key) < 32:
        raise RuntimeError("MCP 身份验证密钥长度不足")

    return key



def issue_identity_token(user: User, settings: Settings) -> str:
    if user.status not in ACTIVE_STATUSES:
        raise PermissionError("用户已停用")

    now = int(time.time())

    budget = current_budget()
    if budget is None:
        raise RuntimeError("缺少请求截止时间")

    return jwt.encode(
        {
            "sub": user.feishu_open_id,
            "iss": settings.auth_jwt_issuer,
            "aud": MCP_AUDIENCE,
            "iat": now,
            "exp": now + 60,
            "deadline": budget.deadline_unix,
            "request_id": budget.request_id,
        },
        _signing_key(settings),
        algorithm="HS256",
    )

def decode_identity_context(token: str, settings: Settings) -> dict:
    try:
        payload = jwt.decode(
            token, _signing_key(settings), algorithms=["HS256"],
            issuer=settings.auth_jwt_issuer, audience=MCP_AUDIENCE,
            options={"require": ["sub", "iat", "exp", "deadline", "request_id"]},
        )
        if not isinstance(payload["sub"], str) or not payload["sub"]:
            raise jwt.InvalidTokenError()
        if (type(payload["deadline"]) not in (float, int)
                or not time.time() < payload["deadline"] <= time.time() + 5.1
                or not isinstance(payload["request_id"], str)
                or not re.fullmatch(r"[a-f0-9]{32}", payload["request_id"])):
            raise jwt.InvalidTokenError()
        return payload
    except jwt.PyJWTError:
        raise PermissionError("身份凭证无效或已过期") from None


async def resolve_identity_token(
    token: str, settings: Settings, db: AsyncSession,
) -> User:
    open_id = decode_identity_context(token, settings)["sub"]
    user = await db.scalar(
        select(User).where(User.feishu_open_id == open_id)
    )
    if user is None or user.status not in ACTIVE_STATUSES:
        raise PermissionError("用户不存在或已停用")

    return user

class KnowledgeSecurity:
    def __init__(self, settings: Settings):
        self._key = settings.knowledge_log_key.get_secret_value().encode()

        try:
            self._cipher = Fernet(self._key)
        except (ValueError, TypeError):
            raise RuntimeError("KNOWLEDGE_LOG_KEY配置无效") from None

        words = {
            word.strip()
            for word in settings.knowledge_sensitive_words
            if word.strip()
        }
        self._word_pattern = (
            re.compile(
                "|".join(
                    re.escape(word)
                    for word in sorted(words, key=len, reverse=True)
                ),
                re.IGNORECASE,
            )
            if words else None
        )

    def encrypt_query(self, query: str) -> str:
        return self._cipher.encrypt(query.encode("utf-8")).decode("ascii")

    def user_identifier(self, user_id: int) -> str:
        return hmac.new(
            self._key,
            f"user:{user_id}".encode("utf-8"),
            hashlib.sha256,
        ).hexdigest()

    def filter_text(self, text: str) -> str:
        if self._word_pattern is None:
            return text
        return self._word_pattern.sub("[已过滤]", text)