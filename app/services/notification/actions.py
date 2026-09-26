from copy import deepcopy
from uuid import uuid4

from fastapi import HTTPException
from sqlalchemy import select

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.expense import Expense
from app.models.expense_flow import FinanceExpenseMock
from app.models.notification import NotificationItem, NotificationLog
from app.models.requisition import Requisition, RequisitionApproval
from app.models.user import User
from app.schemas.leave import ApprovalInput
from app.security.auth import ACTIVE_STATUSES
from app.services.attendance.leave_cards import reject_form_card
from app.services.attendance.leave_service import LeaveService
from app.services.expense.approval_state import ensure_transition
from app.utils.time import as_utc, utc_now

from .core import _card_for


async def _current_card(db, batch: NotificationLog) -> dict:
    ids = batch.delivery_item_ids or []
    query = select(NotificationItem).where(NotificationItem.log_id == batch.id)
    if ids:
        query = query.where(NotificationItem.id.in_(ids))
    items = list(await db.scalars(query.order_by(NotificationItem.id)))
    title, body, card = _card_for(items, summary=batch.batch_key == "SUMMARY")
    batch.title, batch.body, batch.card = title, body, card
    return card


async def _expense_decide(actor_id: int, expense_id: int, version: str, operation: str, reason: str):
    if not get_settings().expense_mock_enabled:
        raise HTTPException(409, "财务模拟审批未启用")
    async with create_session() as db, db.begin():
        expense = await db.scalar(select(Expense).where(Expense.id == expense_id).with_for_update())
        remote = await db.scalar(select(FinanceExpenseMock).where(
            FinanceExpenseMock.expense_id == expense_id,
        ).with_for_update())
        if not expense or not remote or not remote.approval_state:
            raise HTTPException(404, "报销审批记录不存在")
        state = deepcopy(remote.approval_state)
        if state["current_approver_id"] != actor_id or str(state["version"]) != version:
            raise HTTPException(409, "审批节点已变化")
        target = {
            "submitted": "manager_approved",
            "manager_approved": "finance_approved",
        }.get(remote.status) if operation == "approve" else {
            "submitted": "manager_rejected",
            "manager_approved": "finance_rejected",
        }.get(remote.status)
        if target is None:
            raise HTTPException(409, "当前节点不支持该操作")
        try:
            ensure_transition(remote.status, target)
        except ValueError:
            raise HTTPException(409, "审批状态已变化") from None
        from app.services.business_rules.expense_adapter import validate_fixed_route
        await validate_fixed_route(db, expense, current_status=remote.status)
        route = (expense.payload or {}).get("approval_route")
        next_id = None
        if operation == "approve":
            index = 1 if remote.status == "submitted" else 2
            if route and len(route.get("nodes", [])) > index:
                next_id = route["nodes"][index]["user_id"]
            if next_id is None:
                raise HTTPException(409, "下一审批人尚未配置")
        elif not reason.strip():
            raise HTTPException(422, "驳回必须填写原因")
        await validate_fixed_route(db, expense, target, next_id)
        state["version"] += 1
        event = {
            "version": state["version"], "request_id": uuid4().hex,
            "from_status": remote.status, "to_status": target,
            "approver_id": actor_id, "comment": reason.strip(),
            "current_approver_id": next_id,
            "occurred_at": as_utc(utc_now()).isoformat(),
            "paid_at": None, "paid_amount": None,
        }
        state["events"].append(event)
        state["current_approver_id"] = next_id
        state["node_started_at"] = event["occurred_at"]
        remote.status, remote.approval_state = target, state
    return target


async def _requisition_decide(actor_id: int, requisition_id: int, version: str, operation: str, reason: str):
    settings = get_settings()
    if settings.oa_requisition_mode != "local" or not settings.requisition_mock_enabled:
        raise HTTPException(409, "OA 模拟审批未启用")
    async with create_session() as db, db.begin():
        row = await db.scalar(select(Requisition).where(Requisition.id == requisition_id).with_for_update())
        if not row or row.status not in {"pending", "approving"}:
            raise HTTPException(409, "申领单已经处理")
        if row.approver_id != actor_id or not row.node_started_at or row.node_started_at.isoformat() != version:
            raise HTTPException(409, "审批节点已变化")
        if operation == "reject" and not reason.strip():
            raise HTTPException(422, "驳回必须填写原因")
        row.status = "approved" if operation == "approve" else "rejected"
        row.approver_id = None
        row.node_started_at = None
        db.add(RequisitionApproval(
            requisition_id=requisition_id, approver_id=actor_id,
            action=operation, comment=reason.strip() or None,
        ))
    return row.status


async def handle_card(data: dict) -> dict:
    event = data.get("event") or {}
    value = (event.get("action") or {}).get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    item_id = value.get("item_id")
    operation = value.get("operation")
    if type(item_id) is not int or operation not in {"approve", "reject", "reject_hint"}:
        raise HTTPException(422, "通知操作参数无效")
    async with create_session() as db, db.begin():
        actor = await db.scalar(select(User).where(User.feishu_open_id == open_id))
        item = await db.get(NotificationItem, item_id)
        batch = await db.get(NotificationLog, item.log_id) if item else None
        if not actor or actor.status not in ACTIVE_STATUSES or not batch or batch.target_user_id != actor.user_id:
            raise HTTPException(403, "无权处理该通知")
        if item.scene != "approval_reminder" or value.get("source_id") != item.source_id.partition(":")[2] or value.get("version") != item.source_version:
            raise HTTPException(422, "通知操作与业务事项不匹配")
        if item.acted_at:
            card = await _current_card(db, batch)
            return {"toast": {"type": "info", "content": "已处理"},
                    "card": {"type": "raw", "data": card}}
        kind = item.source_id.partition(":")[0]
        identifier = item.source_id.partition(":")[2]
    if operation == "reject_hint":
        return {"card": {"type": "raw", "data": reject_form_card("填写驳回原因", value)}}
    form_value = (event.get("action") or {}).get("form_value") or {}
    if not isinstance(form_value, dict):
        raise HTTPException(422, "驳回原因格式无效")
    reason = str(form_value.get("reason") or value.get("reason") or "")
    if operation == "reject" and not reason.strip():
        raise HTTPException(422, "驳回必须填写原因")
    if kind == "leave":
        await LeaveService().decide(open_id, identifier, ApprovalInput(
            action=operation, reject_reason=reason,
        ))
    elif kind == "expense":
        await _expense_decide(actor.user_id, int(identifier), item.source_version, operation, reason)
    elif kind == "requisition":
        await _requisition_decide(actor.user_id, int(identifier), item.source_version, operation, reason)
    else:
        raise HTTPException(422, "未知审批类型")
    async with create_session() as db, db.begin():
        row = await db.scalar(select(NotificationItem).where(NotificationItem.id == item_id).with_for_update())
        row.acted_at, row.acted_by, row.acted_action = utc_now(), actor.user_id, operation
        batch = await db.scalar(select(NotificationLog).where(NotificationLog.id == row.log_id).with_for_update())
        batch.read_at = utc_now()
        if batch.status == "SENT":
            batch.status = "READ"
        card = await _current_card(db, batch)
    return {"toast": {"type": "success", "content": "操作成功"},
            "card": {"type": "raw", "data": card}}
