from datetime import datetime, time, timedelta
from decimal import Decimal
from uuid import uuid4

from sqlalchemy import select, update

from app.core.database import create_session
from app.models import Department
from app.models.department import Department
from app.models.leave_request import LeaveRequest
from app.models.leave_workflow import (
    ApprovalRecord,
    LeaveDraft,
    LeaveNotification,
)
from app.models.user import User
from app.schemas.leave import ApprovalInput, LeaveInput
from app.security.auth import ACTIVE_STATUSES
from app.services.attendance.leave_balance_repository import (
    load_leave_balances,
)
from app.services.attendance.leave_calendar import calculate_year_days
from app.services.attendance.leave_cards import request_card
from app.utils.time import (
    as_shanghai,
    as_utc,
    SHANGHAI_TIMEZONE,
    utc_now,
)

class LeaveError(ValueError):
    def __init__(self,message: str, status_code: int = 409):
        super().__init__(message)
        self.status_code = status_code


def date_to_utc(day, *, end: bool = False) -> datetime:
    local_time = time(23, 59, 59) if end else time.min
    local = datetime.combine(day, local_time, SHANGHAI_TIMEZONE)
    return as_utc(local).replace(tzinfo=None)

def total_days(year_days: dict) -> Decimal:
    """
    计算年假天数
    :param year_days:
    :return:
    """
    return (
        sum(
            (Decimal(str(value))
            for value in year_days.values()),
            Decimal("0")
        )
    )

class LeaveService:
    async def _actor(self, db, open_id: str, *, lock: bool = False):
        statement = select(User).where(User.feishu_open_id == open_id)

        if lock:
            statement = statement.with_for_update()

        actor = await db.scalar(statement)

        if actor is None or actor.status not in ACTIVE_STATUSES:
            raise LeaveError("用户不存在或已离职",403)

        return actor

    async def _manager(self,db,user: User) -> User:
        department_id = user.department_id
        visited = set()

        while department_id is not None and department_id not in visited:
            visited.add(department_id)
            department = await db.get(Department, department_id)

            if department is None:
                break

            manager_id = department.manager_user_id
            if manager_id is not None and manager_id != user.user_id:
                manager = await db.get(User, manager_id)
                if manager is not None and manager.status in ACTIVE_STATUSES:
                    return manager

            department_id = department.parent_id

        raise LeaveError("未配置有效审批人",409)

    async def _check_balance(
            self,
            db,
            user_id: int,
            leave_type: str,
            year_days: dict,
            *,
            lock: bool = False,
    ) -> dict:
        if leave_type == "personal":
            return {}

        balances = {}
        for year in sorted(year_days, key=int):
            rows = await load_leave_balances(
                db,
                user_id,
                int(year),
                (leave_type,),
                for_update=lock,
            )
            balance = rows.get(leave_type)
            required = Decimal(str(year_days[year]))

            if balance is None:
                raise LeaveError(f"{year}年度该假别余额尚未配置", 409)

            remaining = balance.remaining_days
            if remaining < required:
                raise LeaveError(
                    f"{year}年度假期余额不足：剩余{remaining}天，"
                    f"本次需要{required}天，还差{required - remaining}天",
                    409,
                )

            balances[year] = balance
        return balances

    async def _check_overlap(self, db, user_id: int, body: LeaveInput):
        existing = await db.scalar(
            select(LeaveRequest.id)
            .where(
                LeaveRequest.user_id == user_id,
                LeaveRequest.status.in_(("pending", "approved")),
                LeaveRequest.start_time <= date_to_utc(body.end_date, end=True),
                LeaveRequest.end_time >= date_to_utc(body.start_date),
            )
            .limit(1)
        )
        if existing is not None:
            raise LeaveError("该日期范围与已有待审批或已通过的请假重叠")

    def serialize(self, row: LeaveRequest, actor_id: int) -> dict:
        return {
            "request_id": row.request_id,
            "user_id": row.user_id,
            "approver_id": row.approver_id,
            "leave_type": row.leave_type,
            "start_date": as_shanghai(row.start_time).date().isoformat(),
            "end_date": as_shanghai(row.end_time).date().isoformat(),
            "duration_days": str(row.duration),
            "reason": row.reason or "",
            "status": row.status,
            "reject_reason": (
                row.reject_reason or ""
                if actor_id == row.user_id else ""
            ),
            "escalated": row.escalated_at is not None,
        }

    def _record(self, db, row, action, operator_id, remark=""):
        db.add(
            ApprovalRecord(
                request_id=row.request_id,
                action=action,
                operator_id=operator_id,
                remark=remark,
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )

    def _enqueue(self, db, row, receiver, kind, payload):
        db.add(
            LeaveNotification(
                event_key=f"{row.request_id}:{kind}:{receiver.user_id}",
                request_id=row.request_id,
                kind=kind,
                receiver_open_id=receiver.feishu_open_id,
                payload=payload,
                status="pending",
                attempts=0,
                next_attempt_at=utc_now(),
                created_at=utc_now(),
                updated_at=utc_now(),
            )
        )

    async def prepare(
            self,
            actor_open_id: str,
            body: LeaveInput,
            *,
            flow_key: str | None = None,
    ) -> dict:
        async with create_session() as db:
            async with db.begin():
                # 同一申请人的预览、提交及审批按统一用户锁顺序执行。
                actor = await self._actor(db, actor_open_id, lock=True)
                if body.start_date < as_shanghai(utc_now()).date():
                    raise LeaveError("开始日期不能早于今天", 422)

                days = await calculate_year_days(
                    db, body.start_date, body.end_date,
                )
                await self._check_balance(
                    db, actor.user_id, body.leave_type, days,
                )
                await self._check_overlap(db, actor.user_id, body)
                manager = await self._manager(db, actor)

                # 重新生成确认卡片后，旧卡片不能再提交。
                await db.execute(
                    update(LeaveDraft)
                    .where(
                        LeaveDraft.user_id == actor.user_id,
                        LeaveDraft.status == "ready",
                    )
                    .values(status="superseded", updated_at=utc_now())
                )

                payload = {
                    **body.model_dump(mode="json"),
                    "year_days": days,
                    "duration_days": str(total_days(days)),
                    "approver_id": manager.user_id,
                    "approver_name": manager.name,
                }
                draft = LeaveDraft(
                    id=uuid4().hex,
                    user_id=actor.user_id,
                    flow_key=flow_key,
                    payload=payload,
                    status="ready",
                    expires_at=utc_now() + timedelta(minutes=30),
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                db.add(draft)
                result = {"draft_id": draft.id, **payload}
            return result

    async def _draft(self, db, actor, draft_id):
        draft = await db.scalar(
            select(LeaveDraft)
            .where(LeaveDraft.id == draft_id)
            .with_for_update()
        )
        if draft is None:
            raise LeaveError("确认信息不存在", 404)
        if draft.user_id != actor.user_id:
            raise LeaveError("只能操作本人的确认信息", 403)
        return draft

    async def submit(self, actor_open_id: str, draft_id: str) -> dict:
        async with create_session() as db:
            async with db.begin():
                actor = await self._actor(db, actor_open_id, lock=True)
                draft = await self._draft(db, actor, draft_id)

                if draft.status == "submitted":
                    row = await db.scalar(
                        select(LeaveRequest).where(
                            LeaveRequest.draft_id == draft.id,
                        )
                    )
                    if row is None:
                        raise LeaveError("申请数据不一致，请联系管理员", 503)
                    return {
                        **self.serialize(row, actor.user_id),
                        "flow_key": draft.flow_key,
                    }

                if draft.status != "ready" or draft.expires_at <= utc_now():
                    raise LeaveError("确认卡片已失效，请重新填写申请")

                body = LeaveInput.model_validate({
                    key: draft.payload[key]
                    for key in ("leave_type", "start_date", "end_date", "reason")
                })
                if body.start_date < as_shanghai(utc_now()).date():
                    raise LeaveError("开始日期已过期，请重新填写申请")

                days = await calculate_year_days(
                    db, body.start_date, body.end_date,
                )
                manager = await self._manager(db, actor)
                if (
                        days != draft.payload["year_days"]
                        or manager.user_id != draft.payload["approver_id"]
                ):
                    raise LeaveError("工作日或审批人发生变化，请重新确认")

                await self._check_balance(
                    db, actor.user_id, body.leave_type, days, lock=True,
                )
                await self._check_overlap(db, actor.user_id, body)

                row = LeaveRequest(
                    request_id="LV" + uuid4().hex.upper(),
                    draft_id=draft.id,
                    user_id=actor.user_id,
                    leave_type=body.leave_type,
                    start_time=date_to_utc(body.start_date),
                    end_time=date_to_utc(body.end_date, end=True),
                    duration=total_days(days),
                    year_days=days,
                    reason=body.reason,
                    status="pending",
                    approver_id=manager.user_id,
                    created_at=utc_now(),
                    updated_at=utc_now(),
                )
                db.add(row)
                await db.flush()

                draft.status = "submitted"
                draft.updated_at = utc_now()
                self._record(db, row, "submit", actor.user_id, "提交申请")

                self._enqueue(
                    db, row, manager, "submitted",
                    {
                        "msg_type": "interactive",
                        "content": request_card(
                            self.serialize(row, manager.user_id),
                            can_approve=True,
                        ),
                    },
                )
                self._enqueue(
                    db, row, actor, "receipt",
                    {
                        "msg_type": "text",
                        "content": {
                            "text": (
                                f"请假申请已提交，编号：{row.request_id}，"
                                "当前状态：待审批。\n"
                                f"查询详情可回复：查询请假 {row.request_id}"
                            )
                        },
                    },
                )
                result = {
                    **self.serialize(row, actor.user_id),
                    "flow_key": draft.flow_key,
                }
            return result

    async def cancel_draft(self, actor_open_id: str, draft_id: str) -> dict:
        async with create_session() as db:
            async with db.begin():
                actor = await self._actor(db, actor_open_id, lock=True)
                draft = await self._draft(db, actor, draft_id)
                if draft.status == "submitted":
                    raise LeaveError("申请已提交，请使用撤销申请")
                if draft.status not in {"ready", "cancelled"}:
                    raise LeaveError("确认卡片已失效")
                draft.status = "cancelled"
                draft.updated_at = utc_now()
                result = {"flow_key": draft.flow_key}
            return result

    async def _locked_request(self, db, request_id: str) -> LeaveRequest:
        # 先读申请人ID，再按“申请人 -> 申请单 -> 余额”统一加锁。
        user_id = await db.scalar(
            select(LeaveRequest.user_id).where(
                LeaveRequest.request_id == request_id,
            )
        )
        if user_id is None:
            raise LeaveError("申请不存在", 404)

        await db.scalar(
            select(User)
            .where(User.user_id == user_id)
            .with_for_update()
        )
        row = await db.scalar(
            select(LeaveRequest)
            .where(LeaveRequest.request_id == request_id)
            .with_for_update()
            .execution_options(populate_existing=True)
        )
        if row is None:
            raise LeaveError("申请不存在", 404)
        return row

    async def decide(
            self,
            actor_open_id: str,
            request_id: str,
            body: ApprovalInput,
    ) -> dict:
        async with create_session() as db:
            async with db.begin():
                actor = await self._actor(db, actor_open_id)
                row = await self._locked_request(db, request_id)

                if row.approver_id != actor.user_id:
                    raise LeaveError("只有指定审批人可以审批", 403)

                target_status = (
                    "approved" if body.action == "approve" else "rejected"
                )
                if row.status == target_status:
                    if (
                            target_status == "rejected"
                            and row.reject_reason != body.reject_reason
                    ):
                        raise LeaveError("申请已拒绝，不能覆盖原拒绝原因")
                    return self.serialize(row, actor.user_id)

                if row.status != "pending":
                    raise LeaveError("申请已经处理，不能重复审批")
                if not row.year_days:
                    raise LeaveError("历史申请缺少扣减快照，请先人工核对")

                if body.action == "approve":
                    balances = await self._check_balance(
                        db,
                        row.user_id,
                        row.leave_type,
                        row.year_days,
                        lock=True,
                    )
                    for year, balance in balances.items():
                        amount = Decimal(str(row.year_days[year]))
                        balance.used_days += amount
                        balance.remaining_days -= amount
                        balance.updated_at = utc_now()
                else:
                    row.reject_reason = body.reject_reason

                row.status = target_status
                row.approve_time = utc_now()
                row.updated_at = utc_now()
                self._record(
                    db, row, body.action, actor.user_id,
                    "审批通过" if body.action == "approve" else "审批拒绝",
                )

                applicant = await db.get(User, row.user_id)
                if applicant is None:
                    raise LeaveError("申请人数据不存在", 503)

                # 拒绝原因只放进申请人的通知。
                self._enqueue(
                    db, row, applicant, target_status,
                    {
                        "msg_type": "interactive",
                        "content": request_card(
                            self.serialize(row, applicant.user_id),
                        ),
                    },
                )
                result = self.serialize(row, actor.user_id)
            return result

    async def cancel(self, actor_open_id: str, request_id: str) -> dict:
        async with create_session() as db:
            async with db.begin():
                actor = await self._actor(db, actor_open_id)
                row = await self._locked_request(db, request_id)

                if row.user_id != actor.user_id:
                    raise LeaveError("只能撤销本人的申请", 403)
                if row.status == "cancelled":
                    return self.serialize(row, actor.user_id)
                if row.status != "pending":
                    raise LeaveError("只有待审批申请可以撤销")

                row.status = "cancelled"
                row.updated_at = utc_now()
                self._record(db, row, "cancel", actor.user_id, "申请人撤销")

                manager = await db.get(User, row.approver_id)
                for receiver in (actor, manager):
                    if receiver is None:
                        continue
                    self._enqueue(
                        db, row, receiver, "cancelled",
                        {
                            "msg_type": "interactive",
                            "content": request_card(
                                self.serialize(row, receiver.user_id),
                            ),
                        },
                    )
                result = self.serialize(row, actor.user_id)
            return result

    async def detail(self, actor_open_id: str, request_id: str) -> dict:
        async with create_session() as db:
            actor = await self._actor(db, actor_open_id)
            row = await db.scalar(
                select(LeaveRequest).where(
                    LeaveRequest.request_id == request_id,
                )
            )
            if row is None:
                raise LeaveError("申请不存在", 404)
            if actor.user_id not in {row.user_id, row.approver_id}:
                raise LeaveError("无权查看此申请", 403)

            return {
                **self.serialize(row, actor.user_id),
                "can_approve": row.approver_id == actor.user_id,
            }

    async def list_requests(
            self,
            actor_open_id: str,
            *,
            scope: str = "mine",
            offset: int = 0,
            limit: int = 20,
    ) -> dict:
        async with create_session() as db:
            actor = await self._actor(db, actor_open_id)
            statement = select(LeaveRequest).where(
                LeaveRequest.request_id.is_not(None),
            )
            if scope == "inbox":
                statement = statement.where(
                    LeaveRequest.approver_id == actor.user_id,
                    LeaveRequest.status == "pending",
                )
            elif scope == "mine":
                statement = statement.where(
                    LeaveRequest.user_id == actor.user_id,
                )
            else:
                raise LeaveError("列表范围无效", 422)

            rows = (
                await db.scalars(
                    statement.order_by(LeaveRequest.id.desc())
                    .offset(offset).limit(limit + 1)
                )
            ).all()
            items = []
            for row in rows[:limit]:
                item = self.serialize(row, actor.user_id)
                # 列表只展示摘要，理由通过有权限的详情入口读取。
                item.pop("reason")
                item.pop("reject_reason")
                items.append(item)

            return {
                "items": items,
                "offset": offset,
                "limit": limit,
                "has_more": len(rows) > limit,
            }

        
