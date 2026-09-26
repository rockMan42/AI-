import json

from pydantic import ValidationError

from app.schemas.business_rule import RuleCandidate, RulePreview, RuleUpdate
from app.schemas.skill_result import SkillResult
from app.schemas.permission import AccessDenied, PermissionUnavailable
from app.services.business_rules.common import RuleError
from app.services.business_rules.feishu import (
    confirmation_card,
    confirmation_diff,
    difference_card,
    editor_card,
    rule_detail_card,
    versions_card,
)
from app.services.business_rules.service import RuleService
from app.services.role_mapper import resolve_principal
from app.skill_executor.base import BaseSkillExecutor


class BusinessRulesExecutor(BaseSkillExecutor):
    async def executor(self, context, slots, db):
        service = RuleService()
        rule_type = slots.get("rule_type")
        operation = slots.get("operation") or "view"
        try:
            principal = await resolve_principal(context.open_id)
            current = await service.get(principal, rule_type)
            if operation == "compare":
                start = int(slots.get("from_version") or max(1, current["version"] - 1))
                end = int(slots.get("version") or current["version"])
                changes = await service.difference(principal, rule_type, start, end)
                return SkillResult(
                    True,
                    "规则版本对比",
                    card=difference_card(rule_type, start, end, changes["diff"],
                                         owner_open_id=context.open_id),
                )
            if operation == "edit" and not slots.get("rule_data"):
                return SkillResult(True, "请编辑规则并预览", card=editor_card(current, owner_open_id=context.open_id))
            if operation in {"edit", "rollback", "acknowledge"}:
                version = current["version"]
                summary = str(slots.get("change_summary") or "").strip()
                if not summary:
                    return SkillResult(False, "请提供变更或制度确认的原因。")
                if operation == "rollback":
                    old = await service.get(principal, rule_type, int(slots["version"]))
                    update = RuleUpdate(
                        expected_version=version,
                        rule_data=old["rule_data"],
                        bindings=old["bindings"],
                        change_summary=summary,
                    )
                    body = RuleCandidate(
                        rollback_version=int(slots["version"]),
                        expected_version=version,
                        change_summary=summary,
                    )
                else:
                    raw = slots.get("rule_data", current["rule_data"])
                    data = json.loads(raw) if isinstance(raw, str) else raw
                    bindings = current["bindings"]
                    if operation == "acknowledge":
                        bindings = [
                            {
                                **b,
                                "acknowledged_revision": next(
                                    s["observed_revision"]
                                    for s in current["document_status"]
                                    if s["doc_id"] == b["doc_id"]
                                ),
                            }
                            for b in bindings
                        ]
                    update = RuleUpdate(
                        expected_version=version,
                        rule_data=data,
                        bindings=bindings,
                        change_summary=summary,
                    )
                    body = RuleCandidate(
                        update=update, expected_version=version, change_summary=summary
                    )
                preview = await service.preview(
                    principal, rule_type, RulePreview(**update.model_dump())
                )
                if not preview["diff"]:
                    return SkillResult(False, "规则内容没有变化，无需发布。")
                confirmation_diff(preview["diff"])
                candidate = await service.candidate(
                    principal, rule_type, body, "skill:" + context.message_id
                )
                return SkillResult(
                    True,
                    "请核对并确认",
                    card=confirmation_card(
                        rule_type, candidate, preview["diff"], operation == "rollback",
                        expected_version=version,
                    ),
                )
            if operation == "versions":
                versions = await service.versions(principal, rule_type, limit=6)
                return SkillResult(
                    True,
                    "规则历史版本",
                    card=versions_card(rule_type, versions, owner_open_id=context.open_id),
                )
            return SkillResult(
                True,
                "当前规则",
                card=rule_detail_card(current, owner_open_id=context.open_id),
            )
        except (RuleError, ValidationError, ValueError, KeyError,
                AccessDenied, PermissionUnavailable) as exc:
            return SkillResult(
                False,
                str(exc)
                if isinstance(exc, (RuleError, AccessDenied, PermissionUnavailable))
                else "规则参数无效，请补齐类型、内容或版本。",
            )
