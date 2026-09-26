"""业务规则卡片的中文展示及逐项表单转换，不改变规则存储格式。"""
from copy import deepcopy
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

from app.services.business_rules.common import RuleError
from app.services.business_rules.validation import ordered_nodes

LABELS = {
    "attendance": "考勤规则", "approval": "审批规则", "reimbursement": "报销规则",
    "rule_data": "规则内容", "bindings": "关联制度", "policy": "报销政策", "rules": "报销标准",
    "timezone": "时区", "start_time": "上班时间", "end_time": "下班时间",
    "late_threshold_minutes": "迟到宽限（分钟）", "early_leave_threshold_minutes": "早退宽限（分钟）",
    "chains": "审批路线", "default_chain": "默认审批路线", "nodes": "审批步骤", "edges": "步骤顺序",
    "id": "编号", "request_type": "申请类型", "min_amount": "金额下限（元）", "max_amount": "金额上限（元）",
    "stage": "审批环节", "actor": "审批人", "kind": "审批人来源", "user_id": "员工编号",
    "from": "前一步骤", "to": "后一步骤", "rule_name": "规则名称", "rule_category": "费用分类",
    "city_tier": "城市等级", "position_level": "适用职级", "expense_type": "费用类型",
    "effective_date": "生效日期", "expiry_date": "失效日期", "status": "启用状态", "version": "版本",
    "scope": "适用范围", "conflict_group": "规则分组", "priority": "优先级",
    "condition_json": "适用条件", "field": "判断项目", "operator": "判断方式", "value": "判断标准",
    "all": "全部满足", "any": "任一满足", "not": "不满足", "error_message": "不符合时的提示",
    "suggestion": "处理建议", "severity": "处理方式", "allow_override": "允许超标申请",
    "company_name": "公司名称", "company_tax_id": "公司税号", "tier1_cities": "一线城市",
    "position_map": "职级对应关系", "date_tolerance_days": "日期宽限（天）",
    "doc_id": "文档编号", "doc_title": "制度名称", "acknowledged_revision": "已确认修订号",
    "amount": "费用金额", "nightly_amount": "每晚住宿金额", "meal_daily_amount": "每日餐费",
    "buyer_name": "发票抬头", "buyer_tax_id": "购买方税号", "invoice_date": "开票日期",
    "city": "城市", "seat": "座席或舱位",
}
VALUES = {
    "Asia/Shanghai": "北京时间", "manager": "经理", "finance": "财务", "cashier": "出纳",
    "direct_manager": "直属主管", "user": "指定员工", "reimbursement": "报销",
    "active": "启用", "inactive": "停用", "all": "全部", "tier1": "一线城市", "tier2": "其他城市",
    "director": "总监", "supervisor": "主管", "employee": "员工", "hotel": "住宿", "meal": "餐费",
    "train": "火车", "flight": "飞机", "taxi": "出租车", "other": "其他", "accommodation": "住宿",
    "meals": "餐饮", "transport": "交通", "invoice": "单张发票", "trip": "整次出差",
    "block": "阻止提交", "warn": "提示确认", "lte": "不大于", "gte": "不小于", "lt": "小于",
    "gt": "大于", "eq": "等于", "in": "属于", "between": "介于", "contains": "包含",
    "not_contains": "不包含", "date_between": "日期介于", "$company_name": "公司名称",
    "$company_tax_id": "公司税号", "$trip_window": "出差日期范围", "$max_amount": "本规则金额上限",
}
CHOICES = {
    "stage": ["manager", "finance", "cashier"], "kind": ["direct_manager", "user"],
    "request_type": ["reimbursement"], "timezone": ["Asia/Shanghai"],
    "status": ["active", "inactive"], "city_tier": ["tier1", "tier2", "all"],
    "position_level": ["director", "manager", "supervisor", "employee", "all"],
    "expense_type": ["all", "hotel", "meal", "train", "flight", "taxi", "other"],
    "rule_category": ["all", "accommodation", "meals", "transport", "other"],
    "scope": ["invoice", "trip"], "severity": ["block", "warn"],
    "operator": ["lte", "gte", "lt", "gt", "eq", "in", "between", "contains", "not_contains", "date_between"],
    "field": ["amount", "nightly_amount", "meal_daily_amount", "buyer_name", "buyer_tax_id", "invoice_date", "city", "seat", "position_level"],
}


def label(key):
    return LABELS.get(str(key), str(key))


def value_text(value, key=""):
    if value is None:
        return "不限" if key in {"max_amount", "expiry_date"} else "未设置"
    if isinstance(value, bool):
        return "是" if value else "否"
    if isinstance(value, dict):
        if key == "position_map":
            return "\n".join(
                f"{VALUES.get(str(k), str(k))}：{VALUES.get(str(v), str(v))}"
                for k, v in value.items()
            ) or "未设置"
        if key == "condition_json":
            return "；".join(f"{label(k)}：{value_text(v, k)}" for k, v in value.items())
        return "\n".join(f"{label(k)}：{value_text(v, k)}" for k, v in value.items()) or "未设置"
    if isinstance(value, list):
        if key == "tier1_cities":
            return "、".join(str(item) for item in value) or "未设置"
        return "\n".join(f"第 {i + 1} 项：{value_text(v, key)}" for i, v in enumerate(value)) or "无"
    text = str(value)
    if key == "stage" and text == "manager":
        return "主管"
    if key in CHOICES or key == "value" or key == "position_map":
        return VALUES.get(text, LABELS.get(text, text))
    return text


def diff_text(changes):
    lines = []
    def append_change(path, before, after):
        if isinstance(before, list) and isinstance(after, list):
            for i in range(max(len(before), len(after))):
                a = before[i] if i < len(before) else None
                b = after[i] if i < len(after) else None
                if a != b:
                    append_change([*path, f"第 {i + 1} 项"], a, b)
            return
        if isinstance(before, dict) and isinstance(after, dict):
            for key in sorted(before.keys() | after.keys()):
                if before.get(key) != after.get(key):
                    append_change([*path, key], before.get(key), after.get(key))
            return
        keys = [p for p in path if p != "rule_data"]
        title = " · ".join(label(p) for p in keys) or "规则内容"
        key = path[-1] if path else ""
        lines.append(f"{title}\n修改前：{value_text(before, key)}\n修改后：{value_text(after, key)}")
    for change in changes:
        path = [f"第 {int(p) + 1} 项" if p.isdigit() else p.replace("~1", "/").replace("~0", "~")
                for p in change["path"].split("/") if p]
        append_change(path, change.get("before"), change.get("after"))
    return "\n\n".join(lines) or "两个版本的规则内容一致，无需修改。"


def history_text(versions):
    entries = []
    for item in versions:
        date = item.get("created_at")
        if isinstance(date, str):
            try:
                date = datetime.fromisoformat(date)
            except ValueError:
                pass
        if isinstance(date, datetime):
            date = (date if date.tzinfo else date.replace(tzinfo=timezone.utc)).astimezone(ZoneInfo("Asia/Shanghai")).strftime("%Y-%m-%d %H:%M（北京时间）")
        text = f"第 {item['version']} 版\n变更说明：{item.get('change_summary') or '未填写'}\n发布时间：{date or '未记录'}\n操作人：员工编号 {item.get('changed_by') or '未记录'}"
        if item.get("rollback_from_version"):
            text += f"\n恢复自：第 {item['rollback_from_version']} 版"
        entries.append(text)
    return "\n\n".join(entries) or "暂无历史版本。"


def editable_fields(data, path=()):
    if isinstance(data, dict):
        for key, child in data.items():
            yield from editable_fields(child, (*path, key))
    elif isinstance(data, list):
        for index, child in enumerate(data):
            yield from editable_fields(child, (*path, index))
    elif path and path[-1] != "default_chain":
        yield path, data


def form_groups(rule_type, section, selected):
    if rule_type == "reimbursement" and section == "policy":
        return [("公司与票据信息", "company"), ("职级与城市", "coverage")]
    if rule_type == "reimbursement" and section == "rules":
        return [("名称与适用范围", "scope"), ("判断条件与金额", "condition"),
                ("处理结果", "outcome"), ("优先级与状态", "management")]
    if rule_type == "approval":
        return [("申请类型与金额范围", "range")] + [
            (f"第 {order + 1} 步 · {value_text(node['stage'], 'stage')}",
             f"node_{selected['nodes'].index(node)}")
            for order, node in enumerate(ordered_nodes(selected))
        ]
    return []


def group_allows(path, group):
    if group is None:
        return True
    key = path[0]
    groups = {
        "company": {"company_name", "company_tax_id", "date_tolerance_days"},
        "coverage": {"position_map", "tier1_cities"},
        "scope": {"rule_name", "rule_category", "expense_type", "city_tier",
                  "position_level", "effective_date", "expiry_date", "scope"},
        "condition": {"condition_json", "max_amount"},
        "outcome": {"severity", "allow_override", "error_message", "suggestion"},
        "management": {"status", "priority", "conflict_group", "version"},
        "range": {"request_type", "min_amount", "max_amount"},
    }
    if group in groups:
        return key in groups[group]
    if group.startswith("node_"):
        return key == "actor"
    return False


def form_inputs(data, group=None):
    result = []
    for index, (path, value) in enumerate(editable_fields(data)):
        if not group_allows(path, group):
            continue
        key = str(path[-1])
        title = " · ".join(f"第 {p + 1} 项" if isinstance(p, int) else label(p) for p in path)
        mandatory_invoice_check = (
            isinstance(data, dict)
            and (data.get("condition_json") or {}).get("field")
            in {"buyer_name", "buyer_tax_id", "invoice_date"}
        )
        if mandatory_invoice_check and key in {"allow_override", "severity"}:
            result.append({"tag": "div", "text": {"tag": "plain_text", "content":
                "允许超标申请：否（发票基础校验不可修改）" if key == "allow_override"
                else "处理方式：阻止提交（发票基础校验不可修改）。如需允许超标申请，请选择金额上限类标准。"}})
            continue
        options = CHOICES.get(key)
        if "position_map" in path:
            options = ["director", "manager", "supervisor", "employee"]
        if isinstance(value, bool):
            options = ["true", "false"]
        if options:
            result.append({
                "tag": "select_static", "name": f"field_{index}",
                "placeholder": {"tag": "plain_text", "content": title},
                "initial_option": str(value).lower() if isinstance(value, bool) else str(value),
                "options": [{"text": {"tag": "plain_text", "content": "是" if v == "true" else "否" if v == "false" else value_text(v, "position_map" if "position_map" in path else key)}, "value": v} for v in options],
            })
            result.insert(len(result) - 1, {"tag": "div", "text": {"tag": "plain_text", "content": title}})
        else:
            # 引用以中文呈现，提交时还原，用户无需输入程序表达式。
            default = value_text(value, key) if isinstance(value, str) and value.startswith("$") else "" if value is None else str(value)
            result.append({"tag": "input", "name": f"field_{index}",
                "label": {"tag": "plain_text", "content": title},
                "default_value": default,
                "placeholder": {"tag": "plain_text", "content": "留空表示不限或未设置" if value is None else "请填写"},
                "required": value is not None and value != ""})
    if group and group.startswith("node_") and "user_id" not in data.get("actor", {}):
        result.append({"tag": "input", "name": "actor_user_id",
                       "label": {"tag": "plain_text", "content": "指定员工编号（选择指定员工时填写）"},
                       "placeholder": {"tag": "plain_text", "content": "直属主管可留空"},
                       "required": False})
    return result


def apply_form(data, fields, group=None):
    result = deepcopy(data)
    visible = {}
    for index, (path, _) in enumerate(editable_fields(data)):
        if not group_allows(path, group):
            continue
        if (isinstance(data, dict)
            and (data.get("condition_json") or {}).get("field")
            in {"buyer_name", "buyer_tax_id", "invoice_date"}
            and path[-1] in {"allow_override", "severity"}):
            continue
        visible[f"field_{index}"] = path
    extra = {"actor_user_id"} if group and group.startswith("node_") and "user_id" not in data.get("actor", {}) else set()
    unexpected = set(fields) - set(visible) - {"change_summary"} - extra
    if unexpected:
        raise RuleError("卡片字段不匹配，请重新打开最新编辑卡片", 409)
    missing = set(visible) - set(fields)
    if missing:
        raise RuleError("表单内容不完整，请检查必填项后重试", 422)
    for index, (path, old) in enumerate(editable_fields(data)):
        if not group_allows(path, group):
            continue
        name = f"field_{index}"
        if name not in fields:
            continue
        raw = fields[name]
        if isinstance(raw, dict):
            raw = raw.get("value", "")
        if isinstance(raw, (list, dict)):
            raise RuleError(f"{label(path[-1])}填写不正确，请检查输入")
        text = str(raw).strip() if raw is not None else ""
        try:
            if isinstance(old, bool):
                if text not in {"true", "false"}:
                    raise ValueError()
                value = text == "true"
            elif isinstance(old, int):
                value = int(text)
            elif isinstance(old, float):
                value = float(text)
            elif old is None and not text:
                value = None
            else:
                value = text
                if isinstance(old, str) and old.startswith("$") and text == value_text(old, str(path[-1])):
                    value = old
        except (ValueError, TypeError):
            raise RuleError(f"{label(path[-1])}填写不正确，请检查输入") from None
        parent = result
        for part in path[:-1]:
            parent = parent[part]
        parent[path[-1]] = value
    if group and group.startswith("node_"):
        actor = result["actor"]
        if actor["kind"] == "direct_manager":
            if str(fields.get("actor_user_id") or "").strip():
                raise RuleError("选择直属主管时，无需填写指定员工编号")
            actor.pop("user_id", None)
        elif actor["kind"] == "user":
            raw_user_id = fields.get("actor_user_id", actor.get("user_id"))
            if isinstance(raw_user_id, dict):
                raw_user_id = raw_user_id.get("value")
            try:
                user_id = int(str(raw_user_id).strip())
                if user_id <= 0:
                    raise ValueError()
            except (TypeError, ValueError):
                raise RuleError("选择指定员工后，请填写有效的员工编号") from None
            actor["user_id"] = user_id
    return result
