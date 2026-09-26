"""现有主动消息的统一投递入口，保留原调用方同步确认语义。"""
from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.notification import NotificationItem, NotificationLog
from app.models.user import User
from app.services.conversation_engine.feishu import send_feishu_card as direct_send
from app.utils.time import utc_now

from .core import deliver_one, enqueue


async def send_feishu_card(open_id: str, card: dict, message_uuid: str) -> str:
    if not get_settings().notification_enabled:
        return await direct_send(open_id, card, message_uuid)
    async with create_session() as db:
        user = await db.scalar(select(User).where(User.feishu_open_id == open_id))
    if user is None:
        raise ValueError("通知接收人不存在")
    title = str(((card.get("header") or {}).get("title") or {}).get("content") or "主动通知")[:256]
    body = next((str(element.get("content")) for element in card.get("elements", [])
                 if element.get("tag") == "markdown"), title)[:4000]
    batch_id = await enqueue(
        scene="legacy", source_id=message_uuid, source_version="1",
        target_user_id=user.user_id, variables={}, title=title, body=body,
        raw_card=card,
    )
    if batch_id is None:
        async with create_session() as db:
            batch_id = await db.scalar(select(NotificationItem.log_id).where(
                NotificationItem.scene == "legacy", NotificationItem.source_id == message_uuid,
            ))
        if batch_id is None:
            raise RuntimeError("通知事项未建立")
    async with create_session() as db, db.begin():
        batch = await db.get(NotificationLog, batch_id)
        if batch.status in {"SENT", "READ"}:
            return batch.feishu_message_id
        batch.next_attempt_at = utc_now()
    await deliver_one(batch_id)
    async with create_session() as db:
        batch = await db.get(NotificationLog, batch_id)
        if batch.status not in {"SENT", "READ"} or not batch.feishu_message_id:
            raise RuntimeError("飞书主动消息投递结果未确认")
        return batch.feishu_message_id
