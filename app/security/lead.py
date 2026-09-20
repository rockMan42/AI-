from datetime import UTC, datetime, timedelta

import jwt

from app.config.settings import get_settings


AUDIENCE = "crm-lead-mcp"


class LeadError(ValueError):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.status_code = status_code


def signing_key() -> str:
    value = get_settings().auth_jwt_secret.get_secret_value()
    if len(value) < 32:
        raise LeadError("CRM 内部身份凭证未配置", 503)
    return value


def issue_crm_token(
    user_id: int,
    tool_name: str,
    lead_id: int | None = None,
) -> str:
    now = datetime.now(UTC)
    return jwt.encode(
        {
            "sub": str(user_id),
            "tool": tool_name,
            "lead_id": lead_id,
            "aud": AUDIENCE,
            "iss": get_settings().auth_jwt_issuer,
            "iat": now,
            "exp": now + timedelta(minutes=2),
        },
        signing_key(),
        algorithm="HS256",
    )


def verify_crm_token(
    token: str,
    tool_name: str,
    lead_id: int | None = None,
) -> int:
    try:
        payload = jwt.decode(
            token,
            signing_key(),
            algorithms=["HS256"],
            audience=AUDIENCE,
            issuer=get_settings().auth_jwt_issuer,
            options={
                "require": [
                    "sub", "tool", "aud", "iss", "iat", "exp",
                ],
            },
        )
        if (
            payload["tool"] != tool_name
            or "lead_id" not in payload
            or payload["lead_id"] != lead_id
        ):
            raise ValueError

        user_id = int(payload["sub"])
        if user_id <= 0:
            raise ValueError
        return user_id
    except (jwt.PyJWTError, TypeError, ValueError):
        raise LeadError("CRM 调用身份无效", 403) from None