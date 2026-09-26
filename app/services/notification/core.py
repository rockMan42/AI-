import asyncio
import logging
from datetime import UTC, datetime, timedelta
from uuid import NAMESPACE_URL, uuid4, uuid5

from jinja2 import StrictUndefined
from jinja2.sandbox import SandboxedEnvironment
from sqlalchemy import func, select

from app.core import redis_client as redis_module
from app.core.database import create_session
from app.models.notification import NotificationItem, NotificationLog, NotificationSchedule, NotificationTemplate
from app.models.user import User
from app.security.auth import ACTIVE_STATUSES
from app.services.conversation_engine.feishu import send_feishu_card, update_feishu_card
from app.utils.time import SHANGHAI_TIMEZONE, as_shanghai, utc_now


log = logging.getLogger(__name__)
environment = SandboxedEnvironment(undefined=StrictUndefined, autoescape=False)
QUEUE_KEY = "dep:notification:due"
SCENES = {
    "approval_reminder", "attendance_alert", "lead_follow", "review_deadline",
}


def render(template: str, variables: dict, *, maximum: int) -> str:
    value = environment.from_string(template).render(**variables).strip()
    if not value or len(value) > maximum:
        raise ValueError("通知模板渲染结果为空或过长")
    return value


def notification_card(title: str, body: str, actions: list[dict] | None = None) -> dict:
    card = {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {"tag": "plain_text", "content": title}},
        "elements": [{"tag": "markdown", "content": body}],
    }
    if actions:
        card["elements"].append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "text": {"tag": "plain_text", "content": action["label"]},
                "type": action.get("type", "default"),
                "value": action["value"],
            } for action in actions],
        })
    return card


async def enqueue(
    *, scene: str, source_id: str, source_version: str, target_user_id: int,
    variables: dict, reminder_day=None, action: dict | None = None,
    event_suffix: str = "", title: str | None = None, body: str | None = None,
    raw_card: dict | None = None,
) -> int | None:
    """按事项持久化；相同收件人同一分钟合并，满额进入第20张摘要。"""
    now = utc_now()
    day = reminder_day or as_shanghai(now).date()
    event_key = ":".join((scene, source_id, source_version, str(target_user_id), str(day), event_suffix))
    if len(event_key) > 180:
        event_key = uuid5(NAMESPACE_URL, event_key).hex

    async with create_session() as db, db.begin():
        # 锁用户行使跨进程每日限额和批次选择串行化。
        user = await db.scalar(
            select(User).where(User.user_id == target_user_id).with_for_update()
        )
        if user is None or user.status not in ACTIVE_STATUSES or not user.feishu_open_id:
            return None
        existing = await db.scalar(
            select(NotificationItem.id).where(NotificationItem.event_key == event_key)
        )
        if existing is not None:
            return None

        schedule = await db.scalar(
            select(NotificationSchedule).where(NotificationSchedule.scene == scene)
        )
        if scene in SCENES and (schedule is None or not schedule.enabled):
            return None

        if title is None or body is None:
            template = await db.scalar(
                select(NotificationTemplate).where(
                    NotificationTemplate.scene == scene,
                    NotificationTemplate.is_active.is_(True),
                )
            )
            if template is None:
                raise ValueError(f"通知模板未启用：{scene}")
            title = render(template.title_template, variables, maximum=256)
            body = render(template.body_template, variables, maximum=4000)
            if action and template.actions:
                styles = {entry["operation"]: entry for entry in template.actions}
                action = {"buttons": [
                    {**button, "label": styles[button["value"]["operation"]]["label"],
                     "type": styles[button["value"]["operation"]]["type"]}
                    if button["value"]["operation"] in styles else button
                    for button in action.get("buttons", [])
                ]}

        minute = as_shanghai(now).strftime("%H%M")
        batch_key = "ATTENDANCE" if scene == "attendance_alert" else minute
        batch = await db.scalar(select(NotificationLog).where(
            NotificationLog.target_user_id == target_user_id,
            NotificationLog.send_date == day,
            NotificationLog.batch_key == batch_key,
        ).with_for_update())
        count = await db.scalar(select(func.count(NotificationLog.id)).where(
            NotificationLog.target_user_id == target_user_id,
            NotificationLog.send_date == day,
            NotificationLog.status != "SKIPPED",
        ))
        if batch is not None and batch.first_attempt_at is not None and batch.batch_key not in {"SUMMARY", "ATTENDANCE"}:
            batch = None
            batch_key = f"{minute}:{count}"

        if batch is None or count >= 19:
            if count >= 19:
                batch_key = "SUMMARY"
                batch = await db.scalar(select(NotificationLog).where(
                    NotificationLog.target_user_id == target_user_id,
                    NotificationLog.send_date == day,
                    NotificationLog.batch_key == batch_key,
                ).with_for_update())
            if batch is None:
                # 等一分钟，以便同分钟事件进入同一张卡片。
                next_minute = (now + timedelta(minutes=1)).replace(second=0, microsecond=0)
                batch = NotificationLog(
                    scene=scene if batch_key != "SUMMARY" else "summary",
                    target_user_id=target_user_id, send_date=day, batch_key=batch_key,
                    status="PENDING", next_attempt_at=next_minute,
                    retry_max=schedule.retry_max if schedule else 3,
                    retry_backoff_sec=schedule.retry_backoff_sec if schedule else 60,
                )
                db.add(batch)
                await db.flush()
            elif batch.status in {"SENT", "READ"}:
                batch.status = "UPDATE_PENDING"
                batch.attempts = 0
                batch.next_attempt_at = now
        elif batch.status in {"SENT", "READ"}:
            batch.status = "UPDATE_PENDING"
            batch.attempts = 0
            batch.next_attempt_at = now

        db.add(NotificationItem(
            event_key=event_key, log_id=batch.id, scene=scene,
            source_id=source_id, source_version=source_version,
            title=title, body=body, action=action, raw_card=raw_card, status="PENDING",
        ))
        batch_id, due = batch.id, batch.next_attempt_at

    client = redis_module.redis_client
    if client is not None:
        try:
            await client.zadd(QUEUE_KEY, {str(batch_id): int(due.replace(tzinfo=UTC).timestamp())})
        except Exception:
            log.warning("notification_redis_hint_failed batch_id=%s", batch_id)
    return batch_id


def _card_for(items: list[NotificationItem], *, summary: bool) -> tuple[str, str, dict]:
    def result(item: NotificationItem) -> str:
        return {"approve": "已通过", "reject": "已驳回"}.get(item.acted_action, "已处理")

    if len(items) == 1 and not summary:
        item = items[0]
        if item.acted_at:
            state = result(item)
            suffix = f" · {state}"
            title = item.title[:256 - len(suffix)] + suffix
            result_line = f"\n\n处理结果：{state}"
            body = item.body[:4000 - len(result_line)] + result_line
            card = notification_card(title, body)
            card["header"]["template"] = "green" if item.acted_action == "approve" else "red"
            return title, body, card
        if item.raw_card:
            return item.title, item.body, item.raw_card
        actions = [{**button, "value": {**button["value"], "item_id": item.id}}
                   for button in (item.action or {}).get("buttons", [])]
        return item.title, item.body, notification_card(item.title, item.body, actions)
    title = "今日通知摘要" if summary else "待处理事项提醒"
    lines = [f"- {item.title}：{item.body[:120]}"
             + (f"（{result(item)}）" if item.acted_at else "") for item in items[:20]]
    if len(items) > 20:
        lines.append(f"另有 {len(items) - 20} 项，请在通知列表查看。")
    body = "\n".join(lines)
    card = notification_card(title, body)
    buttons = []
    for item in items:
        if item.acted_at:
            continue
        buttons.extend({**button, "value": {**button["value"], "item_id": item.id}}
                       for button in (item.action or {}).get("buttons", []))
        if item.raw_card:
            for element in item.raw_card.get("elements", []):
                if element.get("tag") == "action":
                    buttons.extend(element.get("actions", []))
    if buttons:
        card["elements"].append({"tag": "action", "actions": buttons[:10]})
    return title, body, card


async def deliver_one(batch_id: int, *, sender=send_feishu_card, updater=update_feishu_card) -> bool:
    now = utc_now()
    token = uuid4().hex
    async with create_session() as db, db.begin():
        batch = await db.scalar(select(NotificationLog).where(
            NotificationLog.id == batch_id,
            NotificationLog.status.in_(("PENDING", "UPDATE_PENDING", "SENDING")),
            NotificationLog.next_attempt_at <= now,
            (NotificationLog.lease_until.is_(None) | (NotificationLog.lease_until < now)),
        ).with_for_update(skip_locked=True))
        if batch is None:
            return False
        if batch.first_attempt_at and now - batch.first_attempt_at >= timedelta(minutes=50) and not batch.feishu_message_id:
            batch.status, batch.error_msg = "UNKNOWN", "delivery_requires_review"
            return True
        user = await db.get(User, batch.target_user_id)
        items = list(await db.scalars(select(NotificationItem).where(
            NotificationItem.log_id == batch.id, NotificationItem.status.in_(("PENDING", "SENT")),
        ).order_by(NotificationItem.id)))
        if not items or user is None or user.status not in ACTIVE_STATUSES:
            batch.status = "SKIPPED"
            return True
        from app.services.notification.scenes import valid_item
        valid = []
        for item in items:
            if item.status == "SENT" or await valid_item(db, item):
                valid.append(item)
            else:
                item.status = "SKIPPED"
        if not valid:
            batch.status = "SKIPPED"
            return True
        title, body, card = _card_for(valid, summary=batch.batch_key == "SUMMARY")
        if batch.card is None:
            batch.title, batch.body, batch.card = title, body, card
            batch.delivery_item_ids = [item.id for item in valid]
        elif batch.feishu_message_id:
            batch.title, batch.body, batch.card = title, body, card
            batch.delivery_item_ids = [item.id for item in valid]
        card = batch.card
        delivered_ids = set(batch.delivery_item_ids)
        message_id = batch.feishu_message_id
        batch.status = "SENDING"
        batch.first_attempt_at = batch.first_attempt_at or now
        batch.attempts += 1
        batch.lease_token, batch.lease_until = token, now + timedelta(seconds=40)
        batch.next_attempt_at = now + timedelta(seconds=40)
        open_id = user.feishu_open_id

    error = None
    try:
        async with asyncio.timeout(20):
            if message_id:
                await updater(message_id, card)
            else:
                message_id = await sender(open_id, card, str(uuid5(NAMESPACE_URL, f"notification:{batch_id}")))
    except Exception as exc:
        error = type(exc).__name__

    async with create_session() as db, db.begin():
        batch = await db.scalar(select(NotificationLog).where(NotificationLog.id == batch_id).with_for_update())
        if batch is None or batch.lease_token != token:
            return True
        batch.lease_token = batch.lease_until = None
        if error:
            batch.error_msg = error
            if batch.attempts >= batch.retry_max + 1:
                batch.status = "FAILED"
                log.error("notification_delivery_failed batch_id=%s error_type=%s", batch_id, error)
            else:
                batch.status = "UPDATE_PENDING" if batch.feishu_message_id else "PENDING"
                delay = batch.retry_backoff_sec * (2 ** (batch.attempts - 1))
                batch.next_attempt_at = utc_now() + timedelta(seconds=delay)
        else:
            batch.feishu_message_id = message_id
            batch.status, batch.error_msg, batch.sent_at = (
                "READ" if batch.read_at else "SENT", None, utc_now()
            )
            current_items = list(await db.scalars(select(NotificationItem).where(
                NotificationItem.log_id == batch_id,
            )))
            for item in current_items:
                if item.status == "PENDING" and item.id in delivered_ids:
                    item.status = "SENT"
            remaining = await db.scalar(select(func.count(NotificationItem.id)).where(
                NotificationItem.log_id == batch_id,
                NotificationItem.status == "PENDING",
            ))
            if remaining or batch.card != card:
                batch.status, batch.next_attempt_at = "UPDATE_PENDING", utc_now()
    return True


async def deliver_due(limit: int = 100, *, sender=send_feishu_card,
                      updater=update_feishu_card) -> int:
    async with create_session() as db:
        ids = list(await db.scalars(select(NotificationLog.id).where(
            NotificationLog.status.in_(("PENDING", "UPDATE_PENDING", "SENDING")),
            NotificationLog.next_attempt_at <= utc_now(),
        ).order_by(NotificationLog.next_attempt_at, NotificationLog.id).limit(limit)))
    delivered = 0
    for batch_id in ids:
        delivered += bool(await deliver_one(batch_id, sender=sender, updater=updater))
    return delivered
