import asyncio
import logging
import time
from contextlib import suppress

from sqlalchemy import select

from app.config.settings import get_settings
from app.core import redis_client as redis_module
from app.core.database import create_session
from app.models.permission import OrganizationState, PermissionAlert
from app.schemas.permission import Role
from app.services.notification.legacy import send_feishu_card
from app.services.organization import sync_organization
from app.services.role_mapper import resolve_principal


log = logging.getLogger(__name__)
_worker = None


async def deliver_alerts():
    async with create_session() as db:
        alerts = (
            await db.scalars(
                select(PermissionAlert)
                .where(PermissionAlert.delivered.is_(False))
                .order_by(PermissionAlert.created_at)
                .limit(20)
            )
        ).all()

    for alert in alerts:
        lock = redis_module.redis_client.lock(
            f"dep:permission:alert:{alert.id}",
            timeout=30,
            blocking=False,
            thread_local=False,
        )
        if not await lock.acquire():
            continue

        try:
            async with asyncio.timeout(20):
                async with create_session() as db:
                    current = await db.get(PermissionAlert, alert.id)
                    if current.delivered:
                        continue

                recipient = await resolve_principal(alert.recipient)
                if recipient.role != Role.HR_ADMIN:
                    raise RuntimeError("告警接收人不是有效 HR")

                card = {
                    "header": {
                        "template": "red",
                        "title": {
                            "tag": "plain_text",
                            "content": "连续越权告警",
                        },
                    },
                    "elements": [{
                        "tag": "markdown",
                        "content": (
                            f"用户 ID：{alert.user_id}\n"
                            "连续 5 次权限校验被拒绝，请检查审计日志。"
                        ),
                    }],
                }

                await send_feishu_card(
                    alert.recipient,
                    card,
                    message_uuid=alert.id,
                )

                async with create_session() as db, db.begin():
                    current = await db.get(PermissionAlert, alert.id)
                    current.delivered = True
        except Exception as exc:
            log.warning(
                "permission_alert_failed alert_id=%s error_type=%s",
                alert.id,
                type(exc).__name__,
            )
        finally:
            with suppress(Exception):
                await lock.release()


async def worker_loop():
    while True:
        try:
            async with create_session() as db:
                state = (
                    await db.execute(
                        select(
                            OrganizationState.dirty,
                            OrganizationState.synced_at,
                        ).where(OrganizationState.id == 1)
                    )
                ).one()

            max_age = get_settings().permission_org_max_age
            refresh_after = max_age * 0.9

            if state.dirty or time.time() - state.synced_at >= refresh_after:
                await sync_organization()

            await deliver_alerts()
        except Exception as exc:
            log.exception(
                "permission_worker_failed detail=%s",
                exc,
            )

        await asyncio.sleep(2)


async def start_permission_worker():
    global _worker

    if not get_settings().permission_alert_open_ids:
        raise RuntimeError("请配置至少一个 HR 告警接收人")

    if _worker is None:
        _worker = asyncio.create_task(
            worker_loop(),
            name="permission-worker",
        )


async def stop_permission_worker():
    global _worker

    task, _worker = _worker, None
    if task is not None:
        task.cancel()
        with suppress(asyncio.CancelledError):
            await task
