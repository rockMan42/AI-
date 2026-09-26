"""从 notification_schedule 同步 Hermes 任务；只在显式运行时修改 Hermes 配置。"""
import asyncio

from app.config.settings import get_settings
from app.core.database import close_db, init_db
from app.services.notification.schedule import sync_all


async def main():
    await init_db(get_settings())
    try:
        await sync_all()
    finally:
        await close_db()


if __name__ == "__main__":
    asyncio.run(main())
