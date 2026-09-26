from datetime import date, datetime, time, timedelta
from decimal import Decimal
from zoneinfo import ZoneInfo

from app.services.business_rules.common import RuleError
from app.services.business_rules.validation import ordered_nodes
from app.utils.time import as_shanghai


def evaluate_condition(condition, facts, rule):
    if "all" in condition:
        return all(evaluate_condition(c, facts, rule) for c in condition["all"])
    if "any" in condition:
        return any(evaluate_condition(c, facts, rule) for c in condition["any"])
    if "not" in condition:
        # 缺失输入不能经 not 转成满足条件。
        from app.services.business_rules.validation import leaves

        for child in leaves(condition["not"]):
            if facts.get(child["field"]) in (None, ""):
                return False
            operand = child["value"]
            if isinstance(operand, str) and operand.startswith("$"):
                value = (
                    rule.get("max_amount")
                    if operand == "$max_amount"
                    else facts.get(operand[1:])
                )
                if value in (None, ""):
                    return False
        return not evaluate_condition(condition["not"], facts, rule)
    from app.services.expense.rules import evaluate_leaf

    return evaluate_leaf({**rule, "condition_json": condition}, facts)


def evaluate_approval(data, context):
    try:
        if isinstance(context.get("amount"), bool):
            raise ValueError()
        amount = Decimal(str(context["amount"]))
        if (
            not amount.is_finite()
            or amount < 0
            or amount != amount.quantize(Decimal("0.01"))
        ):
            raise ValueError()
    except (KeyError, ValueError, ArithmeticError):
        raise RuleError("审批金额必须为非负且最多两位小数") from None
    request_type = context.get("request_type")
    candidates = [
        r
        for r in data["chains"]
        if r["request_type"] == request_type
        and Decimal(r["min_amount"]) <= amount
        and (r["max_amount"] is None or amount < Decimal(r["max_amount"]))
    ]
    route = (
        candidates[0]
        if len(candidates) == 1
        else data.get("default_chain")
        if not candidates
        else None
    )
    if route is None or route["request_type"] != request_type:
        raise RuleError("未配置适用的审批路线", 409, "RULE_NOT_CONFIGURED")
    return {
        "route_id": route["id"],
        "nodes": ordered_nodes(route),
        "explanation": "按申请类型与金额区间选择审批路线",
    }


def evaluate_attendance(data, context):
    zone = ZoneInfo(data["timezone"])
    day = date.fromisoformat(context["work_date"])
    start = datetime.combine(day, time.fromisoformat(data["start_time"]), zone)
    end = datetime.combine(day, time.fromisoformat(data["end_time"]), zone)
    if end <= start:
        end += timedelta(days=1)

    def parse(value):
        if value is None:
            return None
        value = datetime.fromisoformat(value) if isinstance(value, str) else value
        if not isinstance(value, datetime):
            raise RuleError("打卡时间格式错误")
        return as_shanghai(value)

    now = parse(context["now"])
    clock_in, clock_out = (
        parse(context.get("clock_in_time")),
        parse(context.get("clock_out_time")),
    )
    result = {
        "status": "normal",
        "issues": [],
        "late_minutes": 0,
        "early_leave_minutes": 0,
    }

    def issue(value):
        result["issues"].append(value)

    if context.get("is_workday") is None:
        issue("calendar_missing")
    elif not context["is_workday"]:
        result["status"] = "rest"
        return result
    elif context.get("full_day_leave"):
        result["status"] = "leave"
        return result
    elif context.get("partial_leave"):
        issue("partial_leave_review")
    elif (clock_in and clock_in > now) or (clock_out and clock_out > now):
        issue("invalid_punch_order")
    elif clock_in and clock_out and clock_out < clock_in:
        issue("invalid_punch_order")
    else:
        if clock_in:
            minutes = max(0, (clock_in - start).total_seconds() / 60)
            result["late_minutes"] = round(minutes, 2)
            if minutes > data["late_threshold_minutes"]:
                issue("late")
        elif now >= end:
            issue("missing_clock_in")
        if clock_out:
            minutes = max(0, (end - clock_out).total_seconds() / 60)
            result["early_leave_minutes"] = round(minutes, 2)
            if minutes > data["early_leave_threshold_minutes"]:
                issue("early_leave")
        elif now >= end:
            issue("missing_clock_out")
        if not clock_in and not clock_out and now >= end:
            result["status"] = "absent"
            issue("absent")
            return result
        if now < end and not clock_out:
            result["status"] = "pending"
    if any(
        i in result["issues"]
        for i in (
            "calendar_missing",
            "partial_leave_review",
            "invalid_punch_order",
            "missing_clock_in",
            "missing_clock_out",
        )
    ):
        result["status"] = "needs_review"
    elif result["issues"]:
        result["status"] = "late" if "late" in result["issues"] else "early_leave"
    return result


def evaluate_snapshot(snapshot, context):
    rule_type = snapshot["rule_type"]
    if snapshot["schema_version"] != 1 or snapshot["executor_version"] != 1:
        raise RuleError("不支持的历史执行器版本", 409, "RULE_VERSION_UNSUPPORTED")
    if rule_type == "approval":
        result = evaluate_approval(snapshot["rule_data"], context)
    elif rule_type == "attendance":
        result = evaluate_attendance(snapshot["rule_data"], context)
    else:
        from app.services.expense.rules import check_facts, choose_rules

        data = snapshot["rule_data"]
        facts = {
            **context,
            **{
                k: v
                for k, v in data["policy"].items()
                if k in {"company_name", "company_tax_id"}
            },
        }
        today = date.fromisoformat(context["today"])
        rules = [r for r in data["rules"] if r["status"] == "active"]
        selected = choose_rules(rules, facts, today)
        result = {
            "matched_rules": [
                {
                    "id": r.get("id"),
                    "name": r["rule_name"],
                    "max_amount": r.get("max_amount"),
                }
                for r in selected
            ],
            "issues": check_facts(rules, facts, today),
        }
    return {
        **result,
        "rule_type": rule_type,
        "rule_version": snapshot["version"],
        "schema_version": snapshot["schema_version"],
    }
