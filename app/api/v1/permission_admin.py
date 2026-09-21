from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.database import create_session
from app.models.permission import AuditLog, OrganizationState, RoleMappingRule
from app.models.user import User
from app.schemas.permission import PermissionUnavailable, RuleInput
from app.security.auth import get_current_user
from app.security.permission import authorize
from app.services.permission_audit import append_audit
from app.services.role_mapper import resolve_principal


router = APIRouter(prefix="/admin")


def rule_data(rule: RoleMappingRule) -> dict:
    return {
        "id": rule.id,
        "kind": rule.kind,
        "subject": rule.subject,
        "role": rule.role,
        "enabled": rule.enabled,
    }


@router.get("/role-mappings")
async def list_rules(user: User = Depends(get_current_user)):
    principal = await resolve_principal(user.feishu_open_id)
    await authorize(principal, "role.read")

    async with create_session() as db:
        rows = (
            await db.scalars(
                select(RoleMappingRule).order_by(RoleMappingRule.id)
            )
        ).all()
        return [rule_data(row) for row in rows]


@router.put("/role-mappings/{rule_id}")
async def put_rule(
    rule_id: int,
    body: RuleInput,
    user: User = Depends(get_current_user),
):
    if rule_id <= 0:
        raise HTTPException(422, "规则 ID 必须大于零")

    principal = await resolve_principal(user.feishu_open_id)

    # 拒绝日志独立提交；成功修改日志放进修改事务。
    if principal.role.value != "HRAdmin":
        await authorize(principal, "role.write")

    try:
        async with create_session() as db, db.begin():
            state = await db.scalar(
                select(OrganizationState)
                .where(OrganizationState.id == 1)
                .with_for_update()
            )

            if state.dirty or state.version != principal.version:
                raise PermissionUnavailable("权限已变化，请重试")

            rule = await db.get(RoleMappingRule, rule_id)
            before = rule_data(rule) if rule else None

            if rule is None:
                rule = RoleMappingRule(id=rule_id)
                db.add(rule)

            for name, value in body.model_dump(mode="json").items():
                setattr(rule, name, value)

            state.version += 1

            await append_audit(
                db,
                principal.user_id,
                "role.write",
                True,
                rule_id,
                {"before": before, "after": body.model_dump(mode="json")},
            )

            return {"id": rule_id, **body.model_dump(mode="json")}
    except IntegrityError:
        raise HTTPException(409, "该用户或部门已存在映射规则") from None


@router.get("/audit-logs")
async def list_audit_logs(
    user: User = Depends(get_current_user),
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
):
    principal = await resolve_principal(user.feishu_open_id)
    await authorize(principal, "audit.read")

    async with create_session() as db:
        rows = (
            await db.scalars(
                select(AuditLog)
                .order_by(AuditLog.created_at.desc(), AuditLog.id.desc())
                .offset(offset)
                .limit(limit)
            )
        ).all()

        return [
            {
                "id": row.id,
                "user_id": row.user_id,
                "action": row.action,
                "resource_type": row.resource_type,
                "resource_id": row.resource_id,
                "result": row.result,
                "detail": row.detail,
                "ip_address": row.ip_address,
                "created_at": row.created_at,
            }
            for row in rows
        ]