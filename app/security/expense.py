from datetime import UTC, datetime, timedelta

import jwt

from app.config.settings import get_settings
from app.services.expense.rules import ExpenseError


AUDIENCE = "expense-finance-mcp"


def secret():
    value = get_settings().auth_jwt_secret.get_secret_value()
    if len(value) < 32:
        raise ExpenseError("内部身份凭证未配置", 503)
    return value


def issue_finance_token(user_id: int, expense_id: int) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "expense_id": expense_id,
            "aud": AUDIENCE,
            "iss": "ai-digital-employee",
            "iat": now,
            "exp": now + timedelta(minutes=2),
        },
        secret(),
        algorithm="HS256",
    )


def verify_finance_token(token: str, expense_id: int) -> int:
    try:
        data = jwt.decode(
            token,
            secret(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer="ai-digital-employee",
            options={
                "require": ["sub", "expense_id", "iat", "exp"],
            },
        )
        if data["expense_id"] != expense_id:
            raise ValueError
        user_id = int(data["sub"])
        if user_id <= 0:
            raise ValueError
        return user_id
    except (jwt.PyJWTError, ValueError, TypeError):
        raise ExpenseError("财务调用身份无效", 403) from None