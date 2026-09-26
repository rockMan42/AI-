from fastapi import APIRouter, Depends, Header, HTTPException, Query

from app.schemas.business_rule import (
    RuleType,
    RuleUpdate,
    RulePreview,
    RuleCandidate,
    RuleConfirmation,
    RuleBindingsUpdate,
)
from app.security.auth import get_current_user
from app.services.role_mapper import resolve_principal
from app.services.business_rules.common import RuleError
from app.services.business_rules.service import RuleService

router = APIRouter(prefix="/rules")
service = RuleService()


async def actor(user=Depends(get_current_user)):
    return await resolve_principal(user.feishu_open_id)


async def request_key(
    value: str = Header(alias="Idempotency-Key", min_length=1, max_length=128),
):
    return value


async def invoke(operation):
    try:
        return await operation
    except RuleError as exc:
        raise HTTPException(
            exc.status_code,
            {"code": exc.code, "message": str(exc), "fields": exc.fields},
        ) from None


@router.get("")
async def list_rules(principal=Depends(actor)):
    return await invoke(service.list(principal))


@router.get("/_health")
async def health(principal=Depends(actor)):
    return await invoke(service.health(principal))


@router.get("/{rule_type}")
async def get_rule(rule_type: RuleType, principal=Depends(actor)):
    return await invoke(service.get(principal, rule_type))


@router.get("/{rule_type}/schema")
async def rule_schema(rule_type: RuleType, principal=Depends(actor)):
    from app.services.business_rules.service import guard
    from app.services.business_rules.validation import schema_for

    await guard(principal)
    return {"schema_version": 1, "executor_version": 1, "schema": schema_for(rule_type)}


@router.put("/{rule_type}")
async def update_rule(
    rule_type: RuleType,
    body: RuleUpdate,
    principal=Depends(actor),
    key=Depends(request_key),
):
    return await invoke(service.publish(principal, rule_type, body, key))


@router.post("/{rule_type}/preview")
async def preview_rule(
    rule_type: RuleType, body: RulePreview, principal=Depends(actor)
):
    return await invoke(service.preview(principal, rule_type, body))


@router.post("/{rule_type}/change-requests")
async def candidate(
    rule_type: RuleType,
    body: RuleCandidate,
    principal=Depends(actor),
    key=Depends(request_key),
):
    return await invoke(service.candidate(principal, rule_type, body, key))


@router.post("/{rule_type}/confirm")
async def confirm(
    rule_type: RuleType, body: RuleConfirmation, principal=Depends(actor)
):
    return await invoke(service.confirm(principal, rule_type, body))


@router.post("/{rule_type}/rollback")
async def rollback(
    rule_type: RuleType, body: RuleConfirmation, principal=Depends(actor)
):
    return await invoke(service.confirm(principal, rule_type, body, rollback_only=True))


@router.get("/{rule_type}/versions")
async def versions(
    rule_type: RuleType,
    principal=Depends(actor),
    offset: int = Query(0, ge=0),
    limit: int = Query(20, ge=1, le=100),
):
    return await invoke(service.versions(principal, rule_type, offset, limit))


@router.get("/{rule_type}/versions/{version}")
async def version(rule_type: RuleType, version: int, principal=Depends(actor)):
    return await invoke(service.get(principal, rule_type, version))


@router.get("/{rule_type}/diff")
async def difference(
    rule_type: RuleType,
    from_version: int = Query(alias="from", ge=1),
    to_version: int = Query(alias="to", ge=1),
    principal=Depends(actor),
):
    return await invoke(
        service.difference(principal, rule_type, from_version, to_version)
    )


@router.put("/{rule_type}/bindings")
async def bindings(
    rule_type: RuleType,
    body: RuleBindingsUpdate,
    principal=Depends(actor),
    key=Depends(request_key),
):
    async def operation():
        current = await service.get(principal, rule_type)
        update = RuleUpdate(**body.model_dump(), rule_data=current["rule_data"])
        return await service.publish(principal, rule_type, update, key)

    return await invoke(operation())
