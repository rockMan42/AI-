import copy
from decimal import Decimal

from jsonschema import Draft202012Validator, FormatChecker
from pydantic import ValidationError

from app.schemas.expense import (
    ExpensePolicyInput,
    ExpenseRuleInput,
    RULE_FIELDS,
    REFERENCE_FIELDS,
)
from app.services.business_rules.common import RuleError, canonical


def obj(properties, required=None):
    return {
        "type": "object",
        "properties": properties,
        "required": required or list(properties),
        "additionalProperties": False,
    }


MONEY = {"type": "string", "pattern": r"^(0|[1-9][0-9]{0,9})(\.[0-9]{1,2})?$"}
IDENTIFIER = {"type": "string", "minLength": 1, "maxLength": 64}
NODE = obj(
    {
        "id": IDENTIFIER,
        "stage": {"enum": ["manager", "finance", "cashier"]},
        "actor": obj(
            {
                "kind": {"enum": ["direct_manager", "user"]},
                "user_id": {"type": "integer", "minimum": 1},
            },
            ["kind"],
        ),
    }
)
ROUTE = obj(
    {
        "id": IDENTIFIER,
        "request_type": {"const": "reimbursement"},
        "min_amount": MONEY,
        "max_amount": {"anyOf": [MONEY, {"type": "null"}]},
        "nodes": {"type": "array", "minItems": 1, "maxItems": 50, "items": NODE},
        "edges": {
            "type": "array",
            "maxItems": 49,
            "items": obj({"from": IDENTIFIER, "to": IDENTIFIER}),
        },
    }
)
ATTENDANCE = obj(
    {
        "timezone": {"const": "Asia/Shanghai"},
        "start_time": {"type": "string", "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$"},
        "end_time": {"type": "string", "pattern": r"^([01][0-9]|2[0-3]):[0-5][0-9]$"},
        "late_threshold_minutes": {"type": "integer", "minimum": 0, "maximum": 240},
        "early_leave_threshold_minutes": {
            "type": "integer",
            "minimum": 0,
            "maximum": 240,
        },
    }
)
CONDITION = {
    "oneOf": [
        obj(
            {
                "all": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"$ref": "#/$defs/condition"},
                }
            }
        ),
        obj(
            {
                "any": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 100,
                    "items": {"$ref": "#/$defs/condition"},
                }
            }
        ),
        obj({"not": {"$ref": "#/$defs/condition"}}),
        obj(
            {
                "field": {"enum": sorted(RULE_FIELDS)},
                "operator": {
                    "enum": [
                        "lte",
                        "gte",
                        "lt",
                        "gt",
                        "eq",
                        "in",
                        "between",
                        "contains",
                        "not_contains",
                        "date_between",
                    ]
                },
                "value": {},
            }
        ),
    ]
}


def schema_for(rule_type):
    if rule_type == "attendance":
        schema = copy.deepcopy(ATTENDANCE)
    elif rule_type == "approval":
        schema = obj(
            {
                "chains": {"type": "array", "maxItems": 1000, "items": ROUTE},
                "default_chain": {"anyOf": [ROUTE, {"type": "null"}]},
            }
        )
    elif rule_type == "reimbursement":
        rule = ExpenseRuleInput.model_json_schema()
        definitions = rule.pop("$defs", {})
        rule["properties"]["id"] = {"type": "integer", "minimum": 1}
        rule["properties"]["condition_json"] = {"$ref": "#/$defs/condition"}
        rule["properties"]["max_amount"] = {"anyOf": [MONEY, {"type": "null"}]}
        policy = ExpensePolicyInput.model_json_schema()
        definitions.update(policy.pop("$defs", {}))
        definitions["condition"] = CONDITION
        schema = obj(
            {
                "policy": policy,
                "rules": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 1000,
                    "items": rule,
                },
            }
        )
        schema["$defs"] = definitions
    else:
        raise RuleError("不支持的规则类型", 404, "RULE_NOT_FOUND")
    schema["$schema"] = "https://json-schema.org/draft/2020-12/schema"
    return schema


def leaves(condition, depth=1):
    if depth > 8:
        raise RuleError("条件嵌套不得超过 8 层")
    if "all" in condition or "any" in condition:
        for child in condition.get("all", condition.get("any", [])):
            yield from leaves(child, depth + 1)
    elif "not" in condition:
        yield from leaves(condition["not"], depth + 1)
    else:
        yield condition


def ordered_nodes(route):
    nodes = {n["id"]: n for n in route["nodes"]}
    if len(nodes) != len(route["nodes"]):
        raise RuleError("审批节点 ID 重复")
    outgoing, incoming = {}, {}
    for edge in route["edges"]:
        a, b = edge["from"], edge["to"]
        if a not in nodes or b not in nodes or a == b or a in outgoing or b in incoming:
            raise RuleError("审批路线含非法引用、并行分支或汇聚")
        outgoing[a], incoming[b] = b, a
    roots = nodes.keys() - incoming.keys()
    if len(roots) != 1:
        raise RuleError("审批路线必须有唯一入口且无环")
    result, current, seen = [], next(iter(roots)), set()
    while current is not None:
        if current in seen:
            raise RuleError("审批路线含环")
        seen.add(current)
        result.append(nodes[current])
        current = outgoing.get(current)
    if len(result) != len(nodes):
        raise RuleError("审批路线含孤立节点或环")
    if [n["stage"] for n in result] != ["manager", "finance", "cashier"]:
        raise RuleError("当前财务适配器仅支持主管、财务、出纳三个顺序阶段")
    for node in result:
        actor = node["actor"]
        if actor["kind"] == "user" and not actor.get("user_id"):
            raise RuleError("指定审批人时必须填写用户 ID")
        if actor["kind"] == "direct_manager" and (
            node["stage"] != "manager" or "user_id" in actor
        ):
            raise RuleError("直属主管引用仅适用于主管节点且不能同时指定用户")
    return result


def validate_data(rule_type, data):
    data = copy.deepcopy(data)
    try:
        raw = canonical(data)
    except (ValueError, TypeError, RecursionError):
        raise RuleError("规则必须是有效 JSON") from None
    if len(raw.encode()) > 256 * 1024:
        raise RuleError("规则配置不得超过 256 KiB", 413)

    # 在递归 Schema 校验前限制 JSON 深度，防止恶意输入耗尽栈。
    def check_depth(value, depth=0):
        if depth > 32:
            raise RuleError("规则结构嵌套过深")
        for child in (
            value.values()
            if isinstance(value, dict)
            else value
            if isinstance(value, list)
            else []
        ):
            check_depth(child, depth + 1)

    check_depth(data)
    errors = list(
        Draft202012Validator(
            schema_for(rule_type), format_checker=FormatChecker()
        ).iter_errors(data)
    )
    if errors:
        raise RuleError(
            "规则字段校验失败",
            fields=[
                {
                    "path": "/" + "/".join(map(str, e.absolute_path)),
                    "message": "不符合 " + str(e.validator) + " 约束",
                }
                for e in errors[:50]
            ],
        )
    if rule_type == "attendance":
        if data["start_time"] == data["end_time"]:
            raise RuleError("班次起止时间不能相同")
    elif rule_type == "approval":
        ids = set()
        ranges = []
        for route in [
            *data["chains"],
            *([data["default_chain"]] if data["default_chain"] else []),
        ]:
            ordered_nodes(route)
            if route["id"] in ids:
                raise RuleError("审批路线 ID 重复")
            ids.add(route["id"])
            lo, hi = (
                Decimal(route["min_amount"]),
                Decimal(route["max_amount"])
                if route["max_amount"] is not None
                else Decimal("Infinity"),
            )
            if lo >= hi:
                raise RuleError("审批金额区间无效")
            if route in data["chains"]:
                ranges.append((lo, hi))
        ranges.sort()
        if any(a[1] > b[0] for a, b in zip(ranges, ranges[1:])):
            raise RuleError("审批金额区间重叠")
        if not ids:
            raise RuleError("至少配置一条审批路线")
    else:
        data["policy"] = ExpensePolicyInput.model_validate(data["policy"]).model_dump(
            mode="json"
        )
        normalized = []
        for index, rule in enumerate(data["rules"]):
            conditions = list(leaves(rule["condition_json"]))
            try:
                base = {**rule, "condition_json": conditions[0]}
                base.pop("id", None)
                parsed = ExpenseRuleInput.model_validate(base).model_dump(mode="json")
            except ValidationError as exc:
                first = exc.errors(include_url=False)[0]
                reason = str(first.get("msg", "字段不符合规则要求"))
                reason = reason.removeprefix("Value error, ")
                rule_name = rule.get("rule_name") or f"第 {index + 1} 条"
                raise RuleError(
                    f"报销标准「{rule_name}」：{reason}",
                    fields=[{"path": f"/rules/{index}", "message": reason}],
                ) from None
            except IndexError:
                raise RuleError(
                    f"报销标准「{rule.get('rule_name') or f'第 {index + 1} 条'}」缺少判断条件",
                    fields=[{"path": f"/rules/{index}/condition_json", "message": "请填写判断条件"}],
                ) from None
            parsed["condition_json"] = rule["condition_json"]
            parsed["id"] = rule.get("id", index + 1)
            normalized.append(parsed)
        data["rules"] = normalized
        ids, names = set(), set()
        for index, rule in enumerate(data["rules"]):
            identifier = rule.get("id", index + 1)
            if identifier in ids or rule["rule_name"] in names:
                raise RuleError("报销规则 ID 或名称重复")
            ids.add(identifier)
            names.add(rule["rule_name"])
            for condition in leaves(rule["condition_json"]):
                try:
                    value = {**rule, "condition_json": condition}
                    value.pop("id", None)
                    ExpenseRuleInput.model_validate(value)
                    if (
                        condition["field"]
                        in {"buyer_name", "buyer_tax_id", "invoice_date"}
                        and "field" not in rule["condition_json"]
                    ):
                        raise ValueError("基础校验必须是独立正向条件")
                    from app.schemas.expense import NUMERIC_FIELDS

                    operand = condition["value"]
                    if isinstance(operand, str) and operand.startswith("$"):
                        if operand not in REFERENCE_FIELDS:
                            raise ValueError()
                    elif condition["field"] in NUMERIC_FIELDS:
                        for number in (
                            operand if isinstance(operand, list) else [operand]
                        ):
                            if (
                                isinstance(number, bool)
                                or not Decimal(str(number)).is_finite()
                            ):
                                raise ValueError()
                    if condition["operator"] == "between" and not isinstance(
                        operand, str
                    ):
                        if Decimal(str(operand[0])) > Decimal(str(operand[1])):
                            raise ValueError()
                except (ValidationError, ValueError, ArithmeticError):
                    raise RuleError(
                        "报销条件或业务字段不合法",
                        fields=[
                            {
                                "path": f"/rules/{index}",
                                "message": "请检查金额、条件引用及基础校验约束",
                            }
                        ],
                    ) from None
        # 对适用范围有交集的同优先级、同具体度规则提前阻断。
        rules = [r for r in data["rules"] if r["status"] == "active"]
        axes = ("rule_category", "expense_type", "city_tier", "position_level")
        for i, a in enumerate(rules):
            for b in rules[i + 1 :]:
                if (
                    a["conflict_group"] != b["conflict_group"]
                    or a["priority"] != b["priority"]
                ):
                    continue
                if sum(a[k] != "all" for k in axes) != sum(b[k] != "all" for k in axes):
                    continue
                overlap = all(a[k] == b[k] or "all" in (a[k], b[k]) for k in axes)
                dates = max(a["effective_date"], b["effective_date"]) <= min(
                    a.get("expiry_date") or "9999-12-31",
                    b.get("expiry_date") or "9999-12-31",
                )
                if overlap and dates:
                    raise RuleError(
                        "报销规则适用范围重叠且优先级相同", 422, "RULE_CONFLICT"
                    )
    return data
