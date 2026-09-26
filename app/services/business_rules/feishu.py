import asyncio
from uuid import NAMESPACE_URL, uuid5

from app.config.settings import get_settings
from app.services.business_rules.common import RuleError
from app.services.business_rules.presentation import diff_text, form_groups, form_inputs, history_text, label, value_text
from app.services.business_rules.validation import ordered_nodes
from app.services.conversation_engine.feishu import (
    _get_tenant_access_token,
    get_feishu_client,
)

RULE_FORM_REVISION = 3


async def request(method, path, **kwargs):
    if not get_settings().rules_feishu_enabled:
        raise RuleError("规则飞书集成未启用", 503, "FEISHU_DISABLED")
    async with asyncio.timeout(10):
        token = await _get_tenant_access_token()
        response = await get_feishu_client().request(
            method,
            "https://open.feishu.cn/open-apis" + path,
            headers={"Authorization": "Bearer " + token},
            **kwargs,
        )
        response.raise_for_status()
        payload = response.json()
        if payload.get("code") != 0:
            raise RuleError("飞书接口暂不可用或权限不足", 503, "FEISHU_UNAVAILABLE")
        return payload["data"]


async def document_meta(doc_id):
    result = await request("GET", f"/docx/v1/documents/{doc_id}")
    document = result["document"]
    if type(document.get("revision_id")) is not int:
        raise RuleError("飞书文档版本格式异常", 503)
    return document


def plain_card(title, message):
    # 按段落拆开长内容，避免单个文本组件过长，也不截断待确认的变更。
    sections, current = [], ""
    for paragraph in message.split("\n\n"):
        if current and len(current) + len(paragraph) > 3000:
            sections.append(current)
            current = ""
        current += ("\n\n" if current else "") + paragraph
    if current:
        sections.append(current)
    return {
        "header": {"title": {"tag": "plain_text", "content": title}},
        "elements": [
            {"tag": "div", "text": {"tag": "plain_text", "content": section}}
            for section in sections
        ],
    }


def _div(content):
    return {"tag": "div", "text": {"tag": "plain_text", "content": content}}


def _button(title, value):
    return {"tag": "button", "text": {"tag": "plain_text", "content": title},
            "value": {"module": "rules", **value}}


def confirmation_diff(changes):
    # 卡片有组件和正文长度限制；一个交互只展示可完整核对的变更。
    if len(changes) > 25:
        raise RuleError("本次变更超过 25 项，请拆分修改，或通过管理接口核对完整差异", 422)
    summary = diff_text(changes)
    if len(summary) > 15000:
        raise RuleError("本次变更内容过长，请拆分修改，或通过管理接口核对完整差异", 422)
    return summary


def confirmation_card(rule_type, candidate, changes, rollback=False, expected_version=None):
    summary = confirmation_diff(changes)
    card = plain_card(
        "确认回滚规则" if rollback else "确认发布规则",
        f"规则：{label(rule_type)}\n将发布第 {expected_version + 1} 版。"
        "确认有效期 15 分钟，请核对以下变更。\n\n"
        + summary,
    )
    card["elements"].append(
        {
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "text": {
                        "tag": "plain_text",
                        "content": "确认回滚" if rollback else "确认发布",
                    },
                    "type": "danger" if rollback else "primary",
                    "value": {
                        "module": "rules",
                        "action": "confirm",
                        "rule_type": rule_type,
                        "change_id": candidate["change_id"],
                        "token": candidate["token"],
                    },
                    "confirm": {
                        "title": {"tag": "plain_text", "content": "确认操作"},
                        "text": {
                            "tag": "plain_text",
                            "content": "确认后将立即发布新版本。",
                        },
                    },
                }
            ],
        }
    )
    return card


def rule_detail_card(snapshot, page=0, owner_open_id=None):
    rule_type = snapshot["rule_type"]
    data = snapshot["rule_data"]
    if rule_type == "reimbursement":
        total = len(data["rules"]) + 1
    elif rule_type == "approval":
        total = len(data["chains"]) + 1 + bool(data.get("default_chain"))
    else:
        total = 1
    if type(page) is not int or not 0 <= page < total:
        raise RuleError("规则内容页码无效，请重新查询", 422)
    card = plain_card(
        f"{label(rule_type)} · 第 {snapshot['version']} 版",
        f"已发布配置 · 第 {page + 1}/{total} 页。\n"
        f"发送“编辑{label(rule_type)}”可打开编辑表单。",
    )
    if rule_type == "reimbursement":
        if page == 0:
            policy = data["policy"]
            for heading, keys in (
                ("公司与票据", ("company_name", "company_tax_id", "date_tolerance_days")),
                ("适用范围", ("position_map", "tier1_cities")),
            ):
                card["elements"].append(_div(heading + "\n" + "\n".join(
                    f"{label(key)}：{value_text(policy[key], key)}" for key in keys if key in policy)))
        else:
            rule = data["rules"][page - 1]
            card["elements"].append(_div(f"标准 {page} · {rule['rule_name']}"))
            for heading, keys in (
                ("适用范围", ("rule_category", "expense_type", "city_tier", "position_level", "effective_date", "expiry_date", "scope")),
                ("判断与额度", ("condition_json", "max_amount")),
                ("处理结果", ("severity", "allow_override", "error_message", "suggestion")),
                ("管理信息", ("status", "priority", "conflict_group")),
            ):
                content = "\n".join(f"{label(key)}：{value_text(rule[key], key)}" for key in keys if key in rule)
                if content:
                    card["elements"].append(_div(heading + "\n" + content))
    elif rule_type == "approval":
        if page == 0:
            card["elements"].append(_div(f"共 {len(data['chains'])} 条金额路线。金额区间左闭右开，最后一档可不设上限。"))
            card["elements"].append(_div("默认路线：" + ("已配置" if data.get("default_chain") else "未配置；没有匹配金额时会阻止提交")))
        else:
            route = data["chains"][page - 1] if page <= len(data["chains"]) else data["default_chain"]
            name = f"路线 {page} · {route['id']}" if page <= len(data["chains"]) else "默认路线"
            upper = route["max_amount"] or "不限"
            card["elements"].append(_div(f"{name}\n适用申请：报销\n金额：{route['min_amount']} 元（含）至 {upper} 元（不含）"))
            for number, node in enumerate(ordered_nodes(route), 1):
                actor = node["actor"]
                who = "直属主管" if actor["kind"] == "direct_manager" else f"员工编号 {actor.get('user_id', '未设置')}"
                card["elements"].append(_div(f"第 {number} 步 · {value_text(node['stage'], 'stage')}\n审批人：{who}"))
    else:
        card["elements"].append(_div(
            f"上班：{data['start_time']}　下班：{data['end_time']}\n"
            f"迟到宽限：{data['late_threshold_minutes']} 分钟\n"
            f"早退宽限：{data['early_leave_threshold_minutes']} 分钟\n"
            "时间均按北京时间。查询历史考勤也会按当前版本重新计算，原始打卡不变。"))
    if page == 0 and snapshot.get("bindings"):
        statuses = {item["doc_id"]: item for item in snapshot.get("document_status", [])}
        lines = []
        for binding in snapshot["bindings"][:10]:
            state = statuses.get(binding["doc_id"], {})
            revision = state.get("observed_revision")
            acknowledged = binding["acknowledged_revision"]
            detail = "待 HR 确认新版" if revision and revision > acknowledged else "已确认"
            if state.get("error"):
                detail = "文档检查异常"
            lines.append(f"{binding.get('doc_title') or binding['doc_id']}：已确认修订 {acknowledged}，{detail}")
        if len(snapshot["bindings"]) > 10:
            lines.append(f"另有 {len(snapshot['bindings']) - 10} 份制度，请在管理接口查看。")
        card["elements"].append(_div("制度关联\n" + "\n".join(lines)))
    actions = []
    base = {"action": "view_page", "rule_type": rule_type,
            "version": snapshot["version"], "owner_open_id": owner_open_id}
    if page > 0:
        actions.append(_button("上一页", {**base, "page": page - 1}))
    if page + 1 < total:
        actions.append(_button("下一页", {**base, "page": page + 1}))
    if actions:
        card["elements"].append({"tag": "action", "actions": actions})
    card["elements"].append({"tag": "action", "actions": [
        _button("编辑规则", {"action": "open_editor", "rule_type": rule_type,
                          "expected_version": snapshot["version"], "owner_open_id": owner_open_id}),
        _button("历史版本", {"action": "history_page", "rule_type": rule_type,
                          "page": 0, "owner_open_id": owner_open_id}),
    ]})
    return card


def versions_card(rule_type, versions, page=0, owner_open_id=None):
    if type(page) is not int or page < 0:
        raise RuleError("历史页码无效，请重新查询", 422)
    card = plain_card(f"{label(rule_type)} · 历史版本",
                      f"第 {page + 1} 页\n\n{history_text(versions[:5])}")
    actions = []
    base = {"action": "history_page", "rule_type": rule_type,
            "owner_open_id": owner_open_id}
    if page > 0:
        actions.append(_button("上一页", {**base, "page": page - 1}))
    if len(versions) > 5:
        actions.append(_button("下一页", {**base, "page": page + 1}))
    if actions:
        card["elements"].append({"tag": "action", "actions": actions})
    return card


def difference_card(rule_type, from_version, to_version, changes, page=0, owner_open_id=None):
    if type(page) is not int or page < 0 or page >= max(1, (len(changes) + 4) // 5):
        raise RuleError("对比页码无效，请重新发送对比指令", 422)
    body = diff_text(changes[page * 5:(page + 1) * 5])
    if len(body) > 12000:
        raise RuleError("此页差异内容过长，请通过管理接口查看完整对比", 422)
    total = max(1, (len(changes) + 4) // 5)
    card = plain_card(f"{label(rule_type)} · 第 {from_version} → {to_version} 版",
                      f"共 {len(changes)} 项变更 · 第 {page + 1}/{total} 页\n\n{body}")
    actions = []
    base = {"action": "diff_page", "rule_type": rule_type,
            "from_version": from_version, "to_version": to_version,
            "owner_open_id": owner_open_id}
    if page > 0:
        actions.append(_button("上一页", {**base, "page": page - 1}))
    if page + 1 < total:
        actions.append(_button("下一页", {**base, "page": page + 1}))
    if actions:
        card["elements"].append({"tag": "action", "actions": actions})
    return card


def editor_card(snapshot, section=None, index=None, page=0, owner_open_id=None, group=None):
    rule_type = snapshot["rule_type"]
    data = snapshot["rule_data"]
    card = {"schema": "2.0", "config": {"width_mode": "fill"},
            "header": {"title": {"tag": "plain_text", "content": f"编辑{label(rule_type)}"}},
            "body": {"elements": []}}
    elements = card["body"]["elements"]
    if section is None and rule_type != "attendance":
        choices = [("报销政策", "policy", None)] if rule_type == "reimbursement" else []
        group = "rules" if rule_type == "reimbursement" else "chains"
        choices.extend((f"第 {i + 1} 条：{item.get('rule_name') or item.get('id')}", group, i)
                       for i, item in enumerate(data[group]))
        if rule_type == "approval" and data.get("default_chain"):
            choices.append(("默认审批路线", "default_chain", None))
        if type(page) is not int or not 0 <= page < max(1, (len(choices) + 9) // 10):
            raise RuleError("编辑页码无效，请重新发送编辑指令", 422)
        elements.append({"tag": "div", "text": {"tag": "plain_text", "content":
            f"当前第 {snapshot['version']} 版。请选择要修改的部分；每次修改一处，预览并确认后发布。新增或删除条目请使用管理接口。"}})
        for title, target, position in choices[page * 10:(page + 1) * 10]:
            elements.append({"tag": "button",
                "text": {"tag": "plain_text", "content": title},
                "behaviors": [{"type": "callback", "value": {
                    "module": "rules", "action": "open_editor", "rule_type": rule_type,
                    "expected_version": snapshot["version"], "section": target,
                    "index": position, "owner_open_id": owner_open_id}}]})
        if page > 0:
            elements.append({"tag": "button",
                "text": {"tag": "plain_text", "content": "查看上一页"},
                "behaviors": [{"type": "callback", "value": {
                    "module": "rules", "action": "open_editor", "rule_type": rule_type,
                    "expected_version": snapshot["version"], "page": page - 1,
                    "owner_open_id": owner_open_id}}]})
        if len(choices) > (page + 1) * 10:
            elements.append({"tag": "button",
                "text": {"tag": "plain_text", "content": "查看下一页"},
                "behaviors": [{"type": "callback", "value": {
                    "module": "rules", "action": "open_editor", "rule_type": rule_type,
                    "expected_version": snapshot["version"], "page": page + 1,
                    "owner_open_id": owner_open_id}}]})
        return card
    selected = data if section is None else data[section] if index is None else data[section][index]
    groups = form_groups(rule_type, section, selected)
    if groups and group is None:
        if not 0 <= page < max(1, (len(groups) + 7) // 8):
            raise RuleError("编辑页码无效，请重新发送编辑指令", 422)
        elements.append(_div(f"请选择要修改的字段组 · 第 {page + 1}/{(len(groups) + 7) // 8} 页。"))
        base = {"module": "rules", "action": "open_editor", "rule_type": rule_type,
                "expected_version": snapshot["version"], "section": section,
                "index": index, "owner_open_id": owner_open_id}
        for title, target in groups[page * 8:(page + 1) * 8]:
            elements.append({"tag": "button", "text": {"tag": "plain_text", "content": title},
                             "behaviors": [{"type": "callback", "value": {**base, "group": target}}]})
        if page > 0:
            elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "上一页"},
                             "behaviors": [{"type": "callback", "value": {**base, "page": page - 1}}]})
        if (page + 1) * 8 < len(groups):
            elements.append({"tag": "button", "text": {"tag": "plain_text", "content": "下一页"},
                             "behaviors": [{"type": "callback", "value": {**base, "page": page + 1}}]})
        return card
    if groups and group not in {name for _, name in groups}:
        raise RuleError("编辑字段组无效，请重新打开卡片", 422)
    if group and group.startswith("node_"):
        selected = selected["nodes"][int(group[5:])]
    selected_name = (
        selected.get("rule_name") or selected.get("id") or label(section or rule_type)
        if isinstance(selected, dict) else label(rule_type)
    )
    if group:
        selected_name = next(title for title, name in groups if name == group)
    elements.append({"tag": "div", "text": {"tag": "plain_text", "content":
        f"正在编辑：{selected_name}（第 {snapshot['version']} 版）。只修改需要变更的字段；校验并预览后才会出现发布确认。"}})
    inputs = form_inputs(selected, group)
    if len(inputs) > 45:
        raise RuleError("该部分字段过多，请使用管理接口修改", 422)
    elements.append({"tag": "form", "name": "rule_edit", "elements": [
        *inputs,
        {"tag": "input", "name": "change_summary",
         "label": {"tag": "plain_text", "content": "变更原因"}, "required": True},
        {"tag": "button", "name": "preview", "type": "primary", "form_action_type": "submit",
         "text": {"tag": "plain_text", "content": "校验并预览"},
         "behaviors": [{"type": "callback", "value": {
             "module": "rules", "action": "preview", "form_format": "fields",
             "form_revision": RULE_FORM_REVISION,
             "rule_type": rule_type, "expected_version": snapshot["version"],
             "section": section, "index": index, "group": group,
             "owner_open_id": owner_open_id}}]},
    ]})
    return card


async def sync_bitable(snapshot, event_id):
    settings = get_settings()
    if not settings.rules_bitable_app_token or not settings.rules_bitable_table_id:
        raise RuleError("规则多维表格尚未配置", 503, "BITABLE_NOT_CONFIGURED")
    base = f"/bitable/v1/apps/{settings.rules_bitable_app_token}/tables/{settings.rules_bitable_table_id}/records"
    identity = f"{snapshot['rule_type']}:v{snapshot['version']}"
    # 即使前次创建成功但响应丢失，也按稳定业务标识找到已有记录。
    found = await request(
        "POST",
        base + "/search",
        json={
            "filter": {
                "conjunction": "and",
                "conditions": [
                    {"field_name": "规则版本", "operator": "is", "value": [identity]}
                ],
            }
        },
        params={"page_size": 100},
    )
    fields = {
        "规则版本": identity,
        "规则类型": snapshot["rule_type"],
        "版本号": snapshot["version"],
        "内容摘要": snapshot["content_hash"],
        "制度文档": ", ".join(b["doc_id"] for b in snapshot["bindings"]),
    }
    fields.update(
        {
            "修改人": str(snapshot.get("changed_by", "")),
            "修改时间": snapshot.get("published_at", ""),
            "制度状态": snapshot.get("document_status_text", "待检查"),
        }
    )
    records = found.get("items", [])
    if records:
        result = await request(
            "PUT", base + "/" + records[0]["record_id"], json={"fields": fields}
        )
    else:
        result = await request(
            "POST",
            base,
            json={"fields": fields},
            params={"client_token": str(uuid5(NAMESPACE_URL, event_id))},
        )
    return result["record"]["record_id"]
