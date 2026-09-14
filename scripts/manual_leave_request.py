import argparse
import asyncio
import json
import sys
import time

import httpx
import jwt

from app.config.settings import get_settings
from app.core.database import init_db, close_db, create_session
from app.models.user import User
from app.security.auth import ACTIVE_STATUSES


async def main(args):
    settings = get_settings()

    await init_db(settings)
    try:
        async with create_session() as db:
            user = await db.get(User, args.user_id)
            if user is None or user.status not in ACTIVE_STATUSES:
                raise RuntimeError("测试用户不存在或已停用")
            open_id = user.feishu_open_id
    finally:
        await close_db()

    secret = settings.auth_jwt_secret.get_secret_value()
    if len(secret) < 32:
        raise RuntimeError("JWT密钥配置无效")

    now = int(time.time())
    token = jwt.encode(
        {
            "sub": open_id,
            "iat": now,
            "exp": now + 600,
            "iss": settings.auth_jwt_issuer,
            "aud": settings.auth_jwt_audience,
        },
        secret,
        algorithm="HS256",
    )

    url = (
        f"http://127.0.0.1:{args.port}"
        + settings.app_prefix
        + "/attendance/leave-requests"
        + args.path
    )
    payload = json.loads(args.payload) if args.payload else None

    async with httpx.AsyncClient(timeout=15) as client:
        response = await client.request(
            args.method,
            url,
            headers={"Authorization": "Bearer " + token},
            json=payload,
        )

    print(
        f"user_id={args.user_id} HTTP={response.status_code}",
        file=sys.stderr,
    )

    # 标准输出只输出JSON，便于shell提取draft_id、request_id。
    print(json.dumps(response.json(), ensure_ascii=False))

    if response.status_code != args.expect:
        raise SystemExit(
            f"状态码不符：预期{args.expect}，实际{response.status_code}"
        )


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("user_id", type=int)
    parser.add_argument("method", choices=["GET", "POST", "PUT"])
    parser.add_argument("path")
    parser.add_argument("payload", nargs="?")
    parser.add_argument("--port", type=int, default=8001)
    parser.add_argument("--expect", type=int, default=200)
    args = parser.parse_args()

    asyncio.run(main(args))