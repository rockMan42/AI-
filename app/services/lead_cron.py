import logging
from collections import defaultdict
from uuid import NAMESPACE_URL, uuid5

from sqlalchemy import func, select, update

from app.core.database import create_session
from app.models import Lead, LeadFollowUp, User
from app.security.auth import ACTIVE_STATUSES
from app.services.notification.legacy import send_feishu_card
from app.services.holiday_cron import cache, lease
from app.services.lead import (
    CLOSED_STATUSES,
    follow_up_counts,
    lead_data,
    score_parts,
    total_score,
)
from app.services.lead_cards import reminder_card
from app.utils.time import as_shanghai, utc_now


logger = logging.getLogger(__name__)


async def recalculate_priorities() -> dict:
    """
    重新计算线索优先级
    :return:
    """
    async with lease("dep:lead:cron:priority"):
        async with create_session() as db:
            now = utc_now()
            leads = (
                await db.scalars(
                    select(Lead).where(
                        Lead.status.notin_(CLOSED_STATUSES)
                    )
                )
            ).all()

            if not leads:
                return {"recalculated": 0}

            # 聚合统计跟进次数，避免每条线索单独查询。
            interactions = await follow_up_counts(
                db, [lead.id for lead in leads], now,
            )
            scores = [
                (
                    lead.id,
                    total_score(score_parts(
                        lead, interactions.get(lead.id, 0), now,
                    )),
                )
                for lead in leads
            ]
            scores.sort(key=lambda item: (-item[1], item[0]))

            total = len(scores)
            high_end = int(total * 0.2)
            medium_end = int(total * 0.8)

            updates = [
                {
                    "id": lead_id,
                    "priority_score": score,
                    "priority": (
                        "high" if index < high_end
                        else "medium" if index < medium_end
                        else "low"
                    ),
                }
                for index, (lead_id, score) in enumerate(scores)
            ]

            for start in range(0, total, 500):
                await db.execute(
                    update(Lead), updates[start:start + 500],
                )
            await db.commit()
            return {"recalculated": total}


def alert_types(item: dict, mode: str, today) -> list[str]:
    if mode == "due_soon":
        next_time = item["next_follow_up"]
        if next_time:
            from datetime import datetime, timedelta

            follow_date = datetime.fromisoformat(next_time).date()
            if follow_date in {today, today + timedelta(days=1)}:
                return ["due_soon"]
        return []

    result = []
    if item["is_overdue"]:
        result.append("overdue")
    if item["priority"] == "high" and item["status"] in {
        "qualified", "proposal",
    }:
        result.append("high_intent")
    return result


async def send_reminders(mode: str) -> dict:
    """
    发送提醒
    :param mode:
    :return:
    """
    if mode not in {"daily", "due_soon"}:
        raise ValueError("提醒类型无效")

    async with lease(f"dep:lead:cron:reminder:{mode}"):
        now = utc_now()
        today = as_shanghai(now).date()

        async with create_session() as db:
            rows = (
                await db.execute(
                    select(Lead, User.feishu_open_id)
                    .join(User, User.user_id == Lead.assigned_to)
                    .where(
                        Lead.status.notin_(CLOSED_STATUSES),
                        User.status.in_(ACTIVE_STATUSES),
                    )
                    .order_by(Lead.priority_score.desc(), Lead.id)
                )
            ).all()

            groups = defaultdict(list)
            recipients = {}

            for lead, open_id in rows:
                item = lead_data(lead, now)
                pending = []
                for alert_type in alert_types(item, mode, today):
                    key = (
                        f"dep:lead:alert:{lead.assigned_to}:"
                        f"{lead.id}:{alert_type}"
                    )
                    if not await cache().exists(key):
                        pending.append(alert_type)

                if pending:
                    item["alert_types"] = pending
                    groups[lead.assigned_to].append(item)
                    recipients[lead.assigned_to] = open_id

            lead_ids = [
                item["id"]
                for items in groups.values()
                for item in items
            ]
            latest = {}
            if lead_ids:
                latest_ids = (
                    select(func.max(LeadFollowUp.id))
                    .where(LeadFollowUp.lead_id.in_(lead_ids))
                    .group_by(LeadFollowUp.lead_id)
                )
                records = await db.execute(
                    select(LeadFollowUp.lead_id, LeadFollowUp.content)
                    .where(LeadFollowUp.id.in_(latest_ids))
                )
                latest = dict(records.all())

        sent = 0
        failures = []
        for user_id, items in groups.items():
            for item in items:
                item["latest_content"] = latest.get(item["id"], "")[:100]

            # 相同聚合内容重试时沿用消息 UUID。
            signature = ",".join(
                f"{item['id']}:{'+'.join(item['alert_types'])}"
                for item in items
            )
            message_uuid = str(uuid5(
                NAMESPACE_URL,
                f"lead:{mode}:{today}:{user_id}:{signature}",
            ))

            try:
                await send_feishu_card(
                    recipients[user_id],
                    reminder_card(items, mode),
                    message_uuid,
                )
                # 成功发送后，再写24小时去重标记。
                async with cache().pipeline(transaction=True) as pipe:
                    for item in items:
                        for alert_type in item["alert_types"]:
                            pipe.set(
                                f"dep:lead:alert:{user_id}:"
                                f"{item['id']}:{alert_type}",
                                "1",
                                ex=86400,
                            )
                    await pipe.execute()
                sent += 1
            except Exception:
                logger.exception(
                    "lead_reminder_failed mode=%s user_id=%s",
                    mode,
                    user_id,
                )
                failures.append(user_id)

        if failures:
            raise RuntimeError(
                f"{len(failures)} 位负责人的提醒未确认成功"
            )
        return {"users_reminded": sent}
