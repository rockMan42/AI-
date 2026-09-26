import argparse
import asyncio

from app.config.settings import get_settings
from app.core.database import close_db, init_db
from app.core.redis_client import close_redis, init_redis
from app.services.notification.scenes import scan_approval, scan_attendance, scan_leads, scan_reviews


SCANNERS = {
    "approval_reminder": scan_approval,
    "attendance_alert": lambda: scan_attendance(morning=True),
    "lead_follow": scan_leads,
    "review_deadline": scan_reviews,
}


async def run(scene: str):
    settings = get_settings()
    if not settings.notification_enabled:
        return 0
    await init_db(settings)
    try:
        await init_redis(settings)
        try:
            return await SCANNERS[scene]()
        finally:
            await close_redis()
    finally:
        await close_db()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("scene", choices=SCANNERS)
    print(asyncio.run(run(parser.parse_args().scene)))
