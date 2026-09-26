from pydantic import ValidationError

from app.schemas.business_rule import (
    RuleCandidate,
    RuleUpdate,
    RulePreview,
    RuleConfirmation,
)
from app.services.business_rules.common import RuleError
from app.services.business_rules.common import RULE_TYPES
from app.services.business_rules.presentation import apply_form, form_groups, label
from app.services.business_rules.feishu import RULE_FORM_REVISION, confirmation_card, confirmation_diff, difference_card, editor_card, plain_card, rule_detail_card, versions_card
from app.services.business_rules.service import RuleService
from app.services.role_mapper import resolve_principal
from app.schemas.permission import AccessDenied, PermissionUnavailable


async def handle_rule_card(data):
    event = data.get("event") or {}
    action = event.get("action") or {}
    value = action.get("value") or {}
    open_id = (event.get("operator") or {}).get("open_id", "")
    rule_type = value.get("rule_type")
    try:
        if rule_type not in RULE_TYPES:
            raise RuleError("规则类型无效，请重新发送规则指令", 422)
        if value.get("group") is not None and not isinstance(value["group"], str):
            raise RuleError("编辑字段组无效", 422)
        if value.get("section") is not None and not isinstance(value["section"], str):
            raise RuleError("编辑位置无效", 422)
        principal = await resolve_principal(open_id)
        service = RuleService()
        if value.get("owner_open_id") and value["owner_open_id"] != open_id:
            raise RuleError("这张卡片属于其他操作人，请自己发送规则指令", 403)
        if value.get("action") == "confirm":
            confirmation = RuleConfirmation(
                change_id=value.get("change_id"), token=value.get("token")
            )
            result = await service.confirm(principal, rule_type, confirmation)
            card = plain_card(
                "规则发布成功",
                f"{label(rule_type)} · 第 {result['version']} 版\n发布成功，相关展示将陆续更新。",
            )
        elif value.get("action") == "view_page":
            version = value.get("version")
            page = value.get("page")
            if type(version) is not int or type(page) is not int:
                raise RuleError("卡片页码无效，请重新查询")
            snapshot = await service.get(principal, rule_type, version)
            card = rule_detail_card(snapshot, page, open_id)
        elif value.get("action") == "history_page":
            page = value.get("page")
            if type(page) is not int or not 0 <= page <= 100000:
                raise RuleError("历史页码无效，请重新查询")
            versions = await service.versions(principal, rule_type, offset=page * 5, limit=6)
            if not versions:
                raise RuleError("没有更多历史版本", 404)
            card = versions_card(rule_type, versions, page, open_id)
        elif value.get("action") == "diff_page":
            start, end, page = value.get("from_version"), value.get("to_version"), value.get("page")
            if any(type(item) is not int for item in (start, end, page)) or start < 1 or end < 1:
                raise RuleError("版本对比参数无效，请重新发送对比指令", 422)
            changes = await service.difference(principal, rule_type, start, end)
            card = difference_card(rule_type, start, end, changes["diff"], page, open_id)
        elif value.get("action") == "open_editor":
            current = await service.get(principal, rule_type)
            if current["version"] != value.get("expected_version"):
                raise RuleError("规则已更新，请重新发送编辑指令", 409)
            section, index = value.get("section"), value.get("index")
            if section is not None:
                if section not in ({"policy", "rules"} if rule_type == "reimbursement" else {"chains", "default_chain"}):
                    raise RuleError("编辑位置无效", 422)
                target = current["rule_data"].get(section)
                if index is not None and (type(index) is not int or not isinstance(target, list) or not 0 <= index < len(target)):
                    raise RuleError("编辑条目不存在", 422)
                if index is None and not isinstance(target, dict):
                    raise RuleError("编辑部分不存在，请重新发送编辑指令", 422)
            page = value.get("page", 0)
            if type(page) is not int or not 0 <= page <= 100:
                raise RuleError("编辑页码无效，请重新发送编辑指令")
            card = editor_card(current, section, index, page=page,
                               owner_open_id=open_id, group=value.get("group"))
        elif value.get("action") == "preview":
            if value.get("form_revision") != RULE_FORM_REVISION or value.get("form_format") != "fields":
                raise RuleError(f"这张编辑卡片已更新，请重新发送“编辑{label(rule_type)}”并打开新卡片", 409)
            fields = action.get("form_value") or {}
            if not isinstance(fields, dict):
                raise RuleError("表单内容无效，请重新打开编辑卡片")
            current = await service.get(principal, rule_type)
            if current["version"] != value.get("expected_version"):
                raise RuleError("规则已更新，请重新打开编辑卡片", 409)
            section, index, group = value.get("section"), value.get("index"), value.get("group")
            rule_data = current["rule_data"].copy()
            if section is None:
                if rule_type != "attendance":
                    raise RuleError("请选择要编辑的部分，请重新打开卡片", 422)
                if group is not None:
                    raise RuleError("编辑字段组无效", 422)
                rule_data = apply_form(rule_data, fields)
            else:
                if section not in ({"policy", "rules"} if rule_type == "reimbursement" else {"chains", "default_chain"}):
                    raise RuleError("编辑位置无效", 422)
                target = rule_data.get(section)
                if index is None:
                    if not isinstance(target, dict):
                        raise RuleError("编辑位置无效", 422)
                    if group not in {name for _, name in form_groups(rule_type, section, target)}:
                        raise RuleError("编辑字段组无效，请重新打开卡片", 422)
                    if group.startswith("node_"):
                        copied = target.copy()
                        copied["nodes"] = target["nodes"].copy()
                        position = int(group[5:])
                        copied["nodes"][position] = apply_form(target["nodes"][position], fields, group)
                        rule_data[section] = copied
                    else:
                        rule_data[section] = apply_form(target, fields, group)
                else:
                    if type(index) is not int or not isinstance(target, list) or not 0 <= index < len(target):
                        raise RuleError("编辑条目不存在", 422)
                    rule_data[section] = target.copy()
                    selected = target[index]
                    if group not in {name for _, name in form_groups(rule_type, section, selected)}:
                        raise RuleError("编辑字段组无效，请重新打开卡片", 422)
                    if group.startswith("node_"):
                        copied = selected.copy()
                        copied["nodes"] = selected["nodes"].copy()
                        position = int(group[5:])
                        copied["nodes"][position] = apply_form(selected["nodes"][position], fields, group)
                        rule_data[section][index] = copied
                    else:
                        rule_data[section][index] = apply_form(selected, fields, group)
            update = RuleUpdate(
                expected_version=value.get("expected_version"),
                rule_data=rule_data,
                change_summary=fields.get("change_summary", ""),
                bindings=current["bindings"],
            )
            preview = await service.preview(
                principal, rule_type, RulePreview(**update.model_dump())
            )
            if not preview["diff"]:
                raise RuleError("没有检测到变更，请修改字段后再预览", 422)
            confirmation_diff(preview["diff"])
            key = (data.get("header") or {}).get("event_id")
            if not key:
                raise RuleError("卡片事件缺少唯一编号")
            candidate = await service.candidate(
                principal,
                rule_type,
                RuleCandidate(
                    update=update,
                    expected_version=update.expected_version,
                    change_summary=update.change_summary,
                ),
                "card:" + key,
            )
            card = confirmation_card(rule_type, candidate, preview["diff"],
                                     expected_version=update.expected_version)
        else:
            raise RuleError("不支持的规则卡片操作")
        return {
            "toast": {"type": "success", "content": "操作成功"},
            "card": {"type": "raw", "data": card},
        }
    except (RuleError, ValidationError, ValueError, AccessDenied, PermissionUnavailable) as exc:
        message = str(exc) if isinstance(exc, (RuleError, AccessDenied, PermissionUnavailable)) else "卡片字段不合法，请检查输入"
        if isinstance(exc, RuleError) and exc.fields:
            problems = []
            for item in exc.fields[:2]:
                path = [p for p in str(item.get("path", "")).split("/") if p]
                location = " · ".join(
                    f"第 {int(p) + 1} 条" if p.isdigit() else label(p)
                    for p in path
                ) or "整体配置"
                problems.append(f"{location}：{item.get('message', '填写不正确')}")
            message = "；".join(problems)
        return {"toast": {"type": "error", "content": message}}
