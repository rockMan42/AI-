from contextvars import ContextVar
from hashlib import sha256
from uuid import uuid4

from sqlalchemy import select
from sqlalchemy.dialects.mysql import insert

from app.config.settings import get_settings
from app.core.database import create_session
from app.models.permission import AuditLog, DenialCounter, PermissionAlert


audit_request_id = ContextVar("permission_request_id", default="")
audit_ip = ContextVar("permission_ip", default=None)


async def append_audit(
    db,
    user_id: int,
    action: str,
    allowed: bool,
    resource_id=None,
    detail: dict | None = None,
):
    request_id = audit_request_id.get() or uuid4().hex
    result = "SUCCESS" if allowed else "DENIED"
    identity = f"{request_id}:{user_id}:{action}:{resource_id}:{result}"
    audit_id = sha256(identity.encode()).hexdigest()[:32]

    # 按用户串行维护连续拒绝次数。
    statement = insert(DenialCounter).values(user_id=user_id, count=0)
    await db.execute(statement.on_duplicate_key_update(user_id=user_id))

    counter = await db.scalar(
        select(DenialCounter)
        .where(DenialCounter.user_id == user_id)
        .with_for_update()
    )

    if await db.get(AuditLog, audit_id):
        return

    db.add(AuditLog(
        id=audit_id,
        user_id=user_id,
        action=action,
        resource_type=action.split(".", 1)[0],
        resource_id=str(resource_id) if resource_id is not None else None,
        result=result,
        detail=detail or {},
        ip_address=audit_ip.get(),
    ))

    counter.count = 0 if allowed else counter.count + 1

    if counter.count == 5:
        for recipient in set(get_settings().permission_alert_open_ids):
            alert_id = sha256(f"{audit_id}:{recipient}".encode()).hexdigest()[:32]
            db.add(PermissionAlert(
                id=alert_id,
                user_id=user_id,
                recipient=recipient,
                delivered=False,
            ))


async def record_audit(
    user_id: int,
    action: str,
    allowed: bool,
    resource_id=None,
    detail: dict | None = None,
):
    # 不复用业务事务，业务失败不会回滚拒绝日志。
    async with create_session() as db, db.begin():
        await append_audit(
            db, user_id, action, allowed, resource_id, detail,
        )