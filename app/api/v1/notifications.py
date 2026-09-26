from datetime import datetime
from uuid import uuid4

from croniter import croniter
from fastapi import APIRouter, Depends, HTTPException, Query
from jinja2 import Environment, meta
from pydantic import BaseModel, ConfigDict, Field
from sqlalchemy import select

from app.core.database import create_session
from app.config.settings import get_settings
from app.models.notification import NotificationLog, NotificationSchedule, NotificationTemplate
from app.schemas.permission import Principal, Role
from app.security.auth import get_current_user
from app.security.permission import authorize
from app.services.notification.core import SCENES, enqueue
from app.services.role_mapper import resolve_principal
from app.utils.time import utc_now


router = APIRouter(prefix="/notifications")
VARIABLES = {
    "approval_reminder": {"kind", "identifier"},
    "attendance_alert": {"employee_name", "issue", "date", "role"},
    "lead_follow": {"company_name", "days_idle"},
    "review_deadline": {"period", "deadline", "days_left"},
}


class TemplateUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    title_template: str = Field(min_length=1, max_length=256)
    body_template: str = Field(min_length=1, max_length=4000)
    actions: list[dict] = Field(default_factory=list, max_length=3)
    is_active: bool = True


class ScheduleUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    cron_expr: str = Field(min_length=9, max_length=64)
    enabled: bool
    retry_max: int = Field(default=3, ge=0, le=3)
    retry_backoff_sec: int = Field(default=60, ge=1, le=3600)


async def actor(user=Depends(get_current_user)) -> Principal:
    return await resolve_principal(user.feishu_open_id)


async def admin(principal: Principal = Depends(actor)) -> Principal:
    await authorize(principal, "notification.manage")
    return principal


@router.get("")
async def list_notifications(
    principal: Principal = Depends(actor), offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    async with create_session() as db:
        rows = list(await db.scalars(select(NotificationLog).where(
            NotificationLog.target_user_id == principal.user_id,
        ).order_by(NotificationLog.id.desc()).offset(offset).limit(limit + 1)))
    return {"items": [{
        "id": row.id, "scene": row.scene, "title": row.title,
        "body": row.body, "status": row.status,
        "created_at": row.created_at, "sent_at": row.sent_at,
        "read_at": row.read_at,
    } for row in rows[:limit]], "has_more": len(rows) > limit}


@router.put("/{notification_id}/read")
async def read_notification(notification_id: int, principal: Principal = Depends(actor)):
    async with create_session() as db, db.begin():
        row = await db.scalar(select(NotificationLog).where(
            NotificationLog.id == notification_id,
            NotificationLog.target_user_id == principal.user_id,
        ).with_for_update())
        if row is None:
            raise HTTPException(404, "通知不存在")
        if row.status == "SENT":
            row.status, row.read_at = "READ", utc_now()
        return {"id": row.id, "status": row.status}


@router.get("/templates")
async def list_templates(_: Principal = Depends(admin)):
    async with create_session() as db:
        rows = list(await db.scalars(select(NotificationTemplate).order_by(NotificationTemplate.scene)))
    return {"items": [{"id": row.id, "scene": row.scene, "title_template": row.title_template,
                       "body_template": row.body_template, "actions": row.actions, "is_active": row.is_active} for row in rows]}


@router.put("/templates/{template_id}")
async def update_template(template_id: int, body: TemplateUpdate, _: Principal = Depends(admin)):
    async with create_session() as db, db.begin():
        row = await db.get(NotificationTemplate, template_id)
        if row is None:
            raise HTTPException(404, "模板不存在")
        allowed = VARIABLES.get(row.scene)
        if allowed is None:
            raise HTTPException(422, "不支持的通知场景")
        engine = Environment()
        try:
            used = meta.find_undeclared_variables(engine.parse(body.title_template))
            used |= meta.find_undeclared_variables(engine.parse(body.body_template))
        except Exception:
            raise HTTPException(422, "模板语法无效") from None
        if used - allowed:
            raise HTTPException(422, "模板变量不在允许范围内")
        operations = set()
        for action in body.actions:
            if row.scene != "approval_reminder" or set(action) != {"operation", "label", "type"}:
                raise HTTPException(422, "操作按钮不在允许范围内")
            operation, label, button_type = action["operation"], action["label"], action["type"]
            if (operation not in {"approve", "reject_hint"} or operation in operations
                    or not isinstance(label, str) or not 1 <= len(label) <= 20
                    or button_type not in {"default", "primary", "danger"}):
                raise HTTPException(422, "操作按钮不在允许范围内")
            operations.add(operation)
        row.title_template, row.body_template = body.title_template, body.body_template
        row.actions, row.is_active = body.actions, body.is_active
    return {"id": template_id, "updated": True}


@router.get("/config")
async def list_schedules(_: Principal = Depends(actor)):
    async with create_session() as db:
        rows = list(await db.scalars(select(NotificationSchedule).order_by(NotificationSchedule.scene)))
    return {"items": [{"scene": row.scene, "cron_expr": row.cron_expr,
                       "enabled": row.enabled, "retry_max": row.retry_max,
                       "retry_backoff_sec": row.retry_backoff_sec} for row in rows]}


@router.put("/config/{scene}")
async def update_schedule(scene: str, body: ScheduleUpdate, _: Principal = Depends(admin)):
    if scene not in SCENES or not croniter.is_valid(body.cron_expr):
        raise HTTPException(422, "场景或 Cron 表达式无效")
    async with create_session() as db, db.begin():
        row = await db.scalar(select(NotificationSchedule).where(NotificationSchedule.scene == scene).with_for_update())
        if row is None:
            raise HTTPException(404, "调度配置不存在")
        row.cron_expr, row.enabled = body.cron_expr, body.enabled
        row.retry_max, row.retry_backoff_sec = body.retry_max, body.retry_backoff_sec
    from app.services.notification.schedule import sync_scene
    sync_scene(scene, config=row)
    return {"scene": scene, "updated": True}


@router.post("/test")
async def test_notification(_: Principal = Depends(admin)):
    if not get_settings().notification_enabled:
        raise HTTPException(503, "主动通知尚未启用")
    identifier = uuid4().hex
    batch_id = await enqueue(
        scene="test", source_id=identifier, source_version="1",
        target_user_id=_.user_id, variables={},
        title="通知测试", body="这是一条管理员测试通知。",
    )
    return {"notification_id": batch_id}
