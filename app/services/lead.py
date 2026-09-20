




"""
查询、详情、跟进、评分规则集中在这里，HTTP 和 MCP 都调用它。
"""
from datetime import datetime, time, UTC, timedelta
from decimal import Decimal

from fastapi.encoders import jsonable_encoder
from sqlalchemy import func, select, or_, case
from sqlalchemy.ext.asyncio import AsyncSession

from app.models import Lead, User, LeadFollowUp
from app.schemas.lead import LeadQuery, STATUS_LABELS, FollowUpInput
from app.security.auth import MANAGEMENT_ROLES, ACTIVE_STATUSES
from app.security.lead import LeadError
from app.utils.time import SHANGHAI_TIMEZONE, as_shanghai, utc_now, as_utc

CLOSED_STATUSES = ("won", "lost")

def day_start(value) -> datetime:
    return datetime.combine(
        value, time.min, tzinfo=SHANGHAI_TIMEZONE
    ).astimezone(UTC).replace(tzinfo=None)

def elapsed_days(value: datetime, now: datetime) -> int:
    return max(
        0,
        (as_shanghai(now).date() - as_shanghai(value).date()).days
    )

def overdue_condition(now: datetime):
    boundary = day_start(
        as_shanghai(now).date() - timedelta(days=7)
    )
    return (
        Lead.status.notin_(CLOSED_STATUSES)
        & (func.coalesce(Lead.last_follow_up, Lead.created_at) < boundary)
    )

def view_condition(view: str, now: datetime):
    active = Lead.status.notin_(CLOSED_STATUSES)
    today = as_shanghai(now).date()

    if view == "overdue":
        return overdue_condition(now)
    if view == "high_intent":
        return (
            (Lead.priority == "high")
            & Lead.status.in_(("qualified", "proposal"))
        )
    if view == "due_soon":
        return (
            active
            & (Lead.next_follow_up >= day_start(today))
            & (Lead.next_follow_up < day_start(today + timedelta(days=2)))
        )
    return True

def permission_condition(actor: User):
    own = Lead.assigned_to == actor.user_id
    if actor.role not in MANAGEMENT_ROLES or actor.department_id is None:
        return own

    team_ids = select(User.user_id).where(
        User.department_id == actor.department_id
    )
    return or_(own, Lead.assigned_to.in_(team_ids))

def row_data(row) -> dict:
    values = {
        column.key: getattr(row, column.key)
        for column in row.__table__.columns
    }
    return jsonable_encoder(
        values,
        custom_encoder={
            datetime: lambda value: as_shanghai(value).isoformat(),
            Decimal: float,
        },
    )
def lead_data(lead: Lead, now: datetime) -> dict:
    result = row_data(lead)
    phone = lead.contact_phone or ""
    email = lead.contact_email or ""

    result["contact_phone"] = (
        phone[:3] + "****" + phone[-4:]
        if len(phone) >= 7 else ("****" if phone else "")
    )
    local, separator, domain = email.partition("@")
    result["contact_email"] = (
        f"{local[:1]}***@{domain}"
        if separator else ("***" if email else "")
    )

    days = elapsed_days(lead.last_follow_up or lead.created_at, now)
    is_overdue = lead.status not in CLOSED_STATUSES and days > 7
    result.update(
        is_high_priority=lead.priority == "high",
        is_overdue=is_overdue,
        overdue_days=days if is_overdue else 0,
        days_since_follow_up=days,
        never_followed=lead.last_follow_up is None,
    )
    return result


def score_parts(lead: Lead, count: int, now: datetime) -> dict:
    budget = lead.budget or Decimal("0")

    budget_score = (
        100 if budget >= 100 else
        80 if budget >= 50 else
        60 if budget >= 10 else 40
    )
    interaction_score = 20 * (min(count, 4) + 1)

    if lead.last_follow_up is None:
        time_decay_score = 20
    else:
        days = elapsed_days(lead.last_follow_up, now)
        time_decay_score = (
            100 if days <= 3 else
            80 if days <= 7 else
            60 if days <= 14 else
            40 if days <= 30 else 20
        )

    return {
        "budget_score": budget_score,
        "interaction_score": interaction_score,
        "time_decay_score": time_decay_score,
    }


def total_score(parts: dict) -> float:
    return round(
        parts["budget_score"] * 0.35
        + parts["interaction_score"] * 0.35
        + parts["time_decay_score"] * 0.30,
        2,
    )


async def follow_up_counts(
    db: AsyncSession,
    lead_ids: list[int],
    now: datetime,
) -> dict[int, int]:
    if not lead_ids:
        return {}
    rows = await db.execute(
        select(LeadFollowUp.lead_id, func.count())
        .where(
            LeadFollowUp.lead_id.in_(lead_ids),
            LeadFollowUp.created_at >= now - timedelta(days=30),
            LeadFollowUp.created_at <= now,
        )
        .group_by(LeadFollowUp.lead_id)
    )
    return dict(rows.all())


class LeadService:
    async def actor(self, db: AsyncSession, user_id: int) -> User:
        user = await db.get(User, user_id)
        if user is None or user.status not in ACTIVE_STATUSES:
            raise LeadError("用户不存在或已停用", 403)

        return user

    async def load(self,
                   db: AsyncSession,
                   actor: User,
                   lead_id: int,
                   *,
                   for_update: bool = False) -> Lead:
        statement = select(Lead).where(Lead.id == lead_id,permission_condition(actor))

        if for_update:
            statement = statement.with_for_update()

        lead = await db.scalar(statement)

        if lead is None:
            raise LeadError("线索不存在或无权访问", 404)

        return lead

    async def query(self,
                    db: AsyncSession,
                    actor: User,
                    params: LeadQuery) -> dict:
        now = utc_now()
        conditions = [
            permission_condition(actor),
            view_condition(params.view, now)
        ]

        if params.team:
            if actor.role not in MANAGEMENT_ROLES:
                raise LeadError("仅主管可查询团队线索", 403)
        elif params.assigned_to is None:
            conditions.append(Lead.assigned_to == actor.user_id)

        if params.assigned_to is not None:
            conditions.append(Lead.assigned_to == params.assigned_to)
        if params.status:
            conditions.append(Lead.status.in_(params.status.split(",")))
        if params.priority:
            conditions.append(Lead.priority == params.priority)
        if params.source:
            conditions.append(Lead.source == params.source)
        if params.company_name:
            conditions.append(
                Lead.company_name.contains(
                    params.company_name, autoescape=True,
                )
            )
        if params.date_from:
            conditions.append(Lead.created_at >= day_start(params.date_from))
        if params.date_to:
            conditions.append(
                Lead.created_at < day_start(
                    params.date_to + timedelta(days=1)
                )
            )
        if params.last_follow_before:
            conditions.append(
                func.coalesce(Lead.last_follow_up, Lead.created_at)
                < day_start(params.last_follow_before)
            )
        if params.sort_by == "priority_score":
            conditions.append(Lead.status.notin_(CLOSED_STATUSES))

        statistics = dict.fromkeys(STATUS_LABELS, 0)
        rows = await db.execute(select(Lead.status, func.count()).where(*conditions).group_by(Lead.status))
        statistics.update(dict(rows.all()))

        counts = (
            await db.execute(
                select(
                    func.sum(case((Lead.priority == "high", 1), else_=0)),
                    func.sum(case((overdue_condition(now), 1), else_=0)),
                ).where(*conditions)
            )
        ).one()

        priority_order = case(
            (Lead.priority == "high", 0),
            (Lead.priority == "medium", 1),
            else_=2,
        )
        ordering = (
            [Lead.priority_score.desc(), Lead.id]
            if params.sort_by == "priority_score"
            else [
                priority_order,
                func.coalesce(Lead.last_follow_up, Lead.created_at),
                Lead.id,
            ]
        )

        rows = (
            await db.execute(
                select(Lead, User.name)
                .outerjoin(User, User.user_id == Lead.assigned_to)
                .where(*conditions)
                .order_by(*ordering)
                .offset((params.page - 1) * params.page_size)
                .limit(params.page_size)
            )
        ).all()

        interactions = await follow_up_counts(
            db, [lead.id for lead, _ in rows], now,
        )
        leads = []
        for lead, assigned_name in rows:
            item = lead_data(lead, now)
            item["assigned_name"] = assigned_name
            item["score_breakdown"] = score_parts(
                lead, interactions.get(lead.id, 0), now,
            )
            leads.append(item)

        return {
            "user_id": actor.user_id,
            "total": sum(statistics.values()),
            "page": params.page,
            "page_size": params.page_size,
            "statistics": statistics,
            "high_count": int(counts[0] or 0),
            "overdue_count": int(counts[1] or 0),
            "leads": leads,
        }

    async def detail(
            self,
            db: AsyncSession,
            actor: User,
            lead_id: int,
    ) -> dict:
        lead = await self.load(db, actor, lead_id)
        result = lead_data(lead, utc_now())
        owner = (
            await db.get(User, lead.assigned_to)
            if lead.assigned_to is not None else None
        )
        result["assigned_name"] = owner.name if owner else None

        records = (
            await db.execute(
                select(LeadFollowUp, User.name)
                .join(User, User.user_id == LeadFollowUp.user_id)
                .where(LeadFollowUp.lead_id == lead.id)
                .order_by(
                    LeadFollowUp.created_at.desc(),
                    LeadFollowUp.id.desc(),
                )
            )
        ).all()
        result["follow_ups"] = [
            {**row_data(record), "user_name": name}
            for record, name in records
        ]
        return result

    async def follow_up(
            self,
            db: AsyncSession,
            actor: User,
            lead_id: int,
            body: FollowUpInput,
    ) -> dict:
        lead = await self.load(db, actor, lead_id, for_update=True)
        if lead.assigned_to != actor.user_id:
            raise LeadError("只能记录自己负责的线索", 403)
        if body.user_id is not None and body.user_id != actor.user_id:
            raise LeadError("跟进人与当前用户不一致", 403)
        if lead.status in CLOSED_STATUSES:
            raise LeadError("已结束的线索不能继续跟进", 409)

        now = utc_now()
        next_time = (
            as_utc(body.next_follow_up).replace(tzinfo=None)
            if body.next_follow_up else None
        )
        if next_time is not None and next_time <= now:
            raise LeadError("下次跟进时间必须晚于当前时间", 422)

        record = LeadFollowUp(
            lead_id=lead.id,
            user_id=actor.user_id,
            follow_up_type=body.follow_up_type,
            content=body.content,
            outcome=body.outcome,
            next_action=body.next_action,
            next_follow_up=next_time,
            created_at=now,
        )
        db.add(record)
        lead.last_follow_up = now
        lead.next_follow_up = next_time

        await db.flush()
        result = row_data(record)
        await db.commit()
        return result




