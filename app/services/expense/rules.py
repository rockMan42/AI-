import hashlib
import json
import logging
from datetime import date
from decimal import Decimal, InvalidOperation

from pydantic import ValidationError
from sqlalchemy import select

from app.core import redis_client as redis_module
from app.models.expense_flow import ExpenseRule
from app.schemas.expense import (
    ExpenseRuleInput,
    NUMERIC_FIELDS,
)
from app.services.expense.invoice_service import InvoiceError


ExpenseError = InvoiceError
CACHE_KEY = "dep:rule:expense"
CACHE_SCHEMA_VERSION = 5
log = logging.getLogger(__name__)

"""
负责规则加载、表达式解释和冲突选择
"""

def digest(value) -> str:
    raw = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=str,
    )
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def money(value) -> Decimal:
    if value is None or isinstance(value, bool):
        raise ValueError("金额不能为空")

    amount = Decimal(str(value))
    if not amount.is_finite() or amount <= 0:
        raise ValueError("金额必须大于零")
    if amount != amount.quantize(Decimal("0.01")):
        raise ValueError("金额最多保留两位小数")
    if amount > Decimal("9999999999.99"):
        raise ValueError("金额超过允许范围")
    return amount


def invoice_key(code, number) -> str:
    number = str(number or "").strip()
    if not number:
        raise ValueError("发票号码不能为空")
    return digest([str(code or "").strip(), number])


def rule_data(row) -> dict:
    try:
        data = ExpenseRuleInput.model_validate(row).model_dump(mode="json")
    except ValidationError as exc:
        fields = sorted({
            ".".join(map(str, error["loc"])) or "规则内容"
            for error in exc.errors()
        })
        log.warning(
            "expense_rule_invalid rule_id=%s fields=%s",
            row.id, fields,
        )
        message = f"报销规则配置错误（规则ID：{row.id}）：{'、'.join(fields)}"
        for name in ("rule_category", "expense_type", "position_level"):
            if name in fields:
                value = str(getattr(row, name))[:50]
                message += f"；{name} 当前值为 {value!r}"
        raise ExpenseError(message + "，请管理员修正规则", 409) from None
    return {"id": row.id, **data}


async def load_rules(db, revision: int) -> list[dict]:
    client = redis_module.redis_client

    if client is not None:
        try:
            cached = await client.get(CACHE_KEY)
            if cached:
                data = json.loads(cached)
                if (
                    data.get("schema_version") == CACHE_SCHEMA_VERSION
                    and data["revision"] == revision
                ):
                    return data["rules"]
        except Exception:
            # 缓存故障时仍从数据库读取。
            pass

    rows = (
        await db.scalars(
            select(ExpenseRule).where(
                ExpenseRule.status == "active",
            )
        )
    ).all()

    rules = []
    invalid_rules = []
    for row in rows:
        try:
            rules.append(rule_data(row))
        except ExpenseError as exc:
            invalid_rules.append((row.id, str(exc)))

    if invalid_rules:
        # 一次记录全部异常，不缓存或使用不完整的规则集合。
        log.warning(
            "expense_rules_invalid details=%s",
            [message for _, message in invalid_rules],
        )
        if len(invalid_rules) == 1:
            raise ExpenseError(invalid_rules[0][1], 409)
        identifiers = "、".join(str(identifier) for identifier, _ in invalid_rules)
        raise ExpenseError(
            f"报销规则配置错误，共{len(invalid_rules)}条（ID：{identifiers}），"
            "详细字段和值已记录到服务日志，请管理员修正",
            409,
        )

    if client is not None:
        try:
            await client.setex(
                CACHE_KEY,
                300,
                json.dumps(
                    {
                        "schema_version": CACHE_SCHEMA_VERSION,
                        "revision": revision,
                        "rules": rules,
                    },
                    ensure_ascii=False,
                ),
            )
        except Exception:
            pass

    return rules


def evaluate(rule: dict, facts: dict) -> bool:
    condition = rule["condition_json"]
    field = condition["field"]
    operator = condition["operator"]
    left = facts.get(field)
    right = condition["value"]

    if isinstance(right, str) and right.startswith("$"):
        name = right[1:]
        right = (
            rule.get("max_amount")
            if name == "max_amount"
            else facts.get(name)
        )

    if left is None or left == "" or right is None or right == "":
        return False

    try:
        if field in NUMERIC_FIELDS:
            left = Decimal(str(left))
            right = (
                [Decimal(str(value)) for value in right]
                if isinstance(right, list)
                else Decimal(str(right))
            )

        if operator == "date_between":
            left = date.fromisoformat(str(left))
            lower, upper = [
                date.fromisoformat(str(value))
                for value in right
            ]
            return lower <= left <= upper

        if operator == "lte":
            return left <= right
        if operator == "gte":
            return left >= right
        if operator == "lt":
            return left < right
        if operator == "gt":
            return left > right
        if operator == "eq":
            return left == right
        if operator == "in":
            return left in right
        if operator == "between":
            return right[0] <= left <= right[1]
        if operator == "contains":
            return str(right) in str(left)
        if operator == "not_contains":
            return str(right) not in str(left)

    except (ValueError, TypeError, InvalidOperation):
        return False

    return False


def choose_rules(
    rules: list[dict],
    facts: dict,
    today: date,
) -> list[dict]:
    groups = {}

    for rule in rules:
        if rule["effective_date"] > today.isoformat():
            continue
        if rule["expiry_date"] and rule["expiry_date"] < today.isoformat():
            continue
        if rule["scope"] == "trip" and not facts["has_trip"]:
            continue

        if rule["rule_category"] not in {"all", facts["category"]}:
            continue
        if rule["expense_type"] not in {"all", facts["expense_type"]}:
            continue
        if rule["city_tier"] not in {"all", facts["city_tier"]}:
            continue
        if rule["position_level"] not in {"all", facts["position_level"]}:
            continue

        # 同组只选择一条；不同组全部执行。
        group = rule["conflict_group"]
        groups.setdefault(group, []).append(rule)

    selected = []

    for group, candidates in groups.items():
        def rank(rule):
            specificity = sum(
                rule[key] != "all"
                for key in (
                    "rule_category", "expense_type",
                    "city_tier", "position_level",
                )
            )
            return rule["priority"], specificity

        candidates.sort(key=rank, reverse=True)
        if len(candidates) > 1 and rank(candidates[0]) == rank(candidates[1]):
            raise ExpenseError(
                f"规则配置冲突：{group}，请管理员调整优先级",
                409,
            )
        selected.append(candidates[0])

    return selected


def failure(
    name: str,
    message: str,
    suggestion="请补齐信息或替换票据",
    *,
    allow_override=False,
    severity="block",
    actual=None,
    expected=None,
) -> dict:
    return {
        "rule_name": name,
        "severity": severity,
        "message": message,
        "suggestion": suggestion,
        "allow_override": allow_override,
        "actual": actual,
        "expected": expected,
    }


def check_facts(rules: list[dict], facts: dict, today: date) -> list[dict]:
    selected = choose_rules(rules, facts, today)
    fields = {
        rule["condition_json"]["field"]
        for rule in selected
    }

    mandatory = {"buyer_name", "buyer_tax_id"}
    if facts["has_trip"]:
        mandatory.add("invoice_date")

    missing = mandatory - fields
    if missing:
        raise ExpenseError(
            "缺少有效的基础校验规则：" + "、".join(sorted(missing)),
            409,
        )

    # 必须有该类费用的标准，防止未配置规则就默认放行。
    standard_fields = {
        "hotel": {"nightly_amount"},
        "meal": {"meal_daily_amount", "amount"},
        "train": {"seat"},
        "flight": {"seat"},
        "taxi": {"amount"},
        "other": {"amount"},
    }[facts["expense_type"]]

    if not (fields & standard_fields):
        return [
            failure(
                "费用标准配置",
                f"尚未配置适用的 {facts['expense_type']} 报销标准",
                "请管理员配置城市、职级对应的规则",
            )
        ]

    failures = []
    for rule in selected:
        if evaluate(rule, facts):
            continue

        condition = rule["condition_json"]
        expected = condition["value"]
        if isinstance(expected, str) and expected.startswith("$"):
            expected = (
                rule["max_amount"]
                if expected == "$max_amount"
                else facts.get(expected[1:])
            )

        failures.append(
            failure(
                rule["rule_name"],
                rule["error_message"],
                rule["suggestion"],
                allow_override=rule["allow_override"],
                severity=rule["severity"],
                actual=facts.get(condition["field"]),
                expected=expected,
            )
        )

    return failures
