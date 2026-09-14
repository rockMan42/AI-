import asyncio
import logging
from contextlib import AsyncExitStack

from app.config.settings import get_settings
from app.core.database import init_db, close_db
from app.core.redis_client import init_redis, close_redis
from app.services.attendance.leave_notifications import (
    deliver_notifications,
    track_leave_approvals,
)
from app.services.conversation_engine.feishu import (
    init_feishu_client,
    close_feishu_client,
)


async def main():
    settings = get_settings()

    async with AsyncExitStack() as stack:
        await init_db(settings)
        stack.push_async_callback(close_db)

        await init_redis(settings)
        stack.push_async_callback(close_redis)

        await init_feishu_client()
        stack.push_async_callback(close_feishu_client)

        await track_leave_approvals()
        await deliver_notifications(limit=500)


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    try:
        asyncio.run(main())
    except Exception as exc:
        logging.error(
            "leave_tracker_process_failed error_type=%s",
            type(exc).__name__,
        )
        raise SystemExit(1)