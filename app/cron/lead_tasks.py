import argparse
import asyncio
import logging
from contextlib import AsyncExitStack

from app.config.settings import get_settings
from app.core.database import close_db, init_db
from app.core.redis_client import close_redis, init_redis
from app.services.conversation_engine.feishu import (
    close_feishu_client,
    init_feishu_client,
)
from app.services.lead_cron import (
    recalculate_priorities,
    send_reminders,
)


async def main(task: str):
    settings = get_settings()

    async with AsyncExitStack() as stack:
        await init_db(settings)
        stack.push_async_callback(close_db)

        await init_redis(settings)
        stack.push_async_callback(close_redis)

        if task == "priority":
            result = await recalculate_priorities()
        else:
            await init_feishu_client()
            stack.push_async_callback(close_feishu_client)
            result = await send_reminders(task)

        logging.info("lead_task_completed task=%s result=%s", task, result)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "task", choices=["priority", "daily", "due_soon"],
    )
    arguments = parser.parse_args()

    logging.basicConfig(level=logging.INFO)
    asyncio.run(main(arguments.task))