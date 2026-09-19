import re
from datetime import date
from app.services.expense.approval_cards import query_card
from app.services.expense.approval_service import (
    approval_detail,
    latest_expense_id,
)
from app.services.expense.approval_state import STATUS_LABELS
from app.schemas.expense import ExpensePrepareInput, ExpenseSupplement
from app.services.expense.cards import card, safe_text
from app.services.expense.expense_service import ExpenseService
from app.services.expense.rules import ExpenseError


service = ExpenseService()

CATEGORY_NAMES = {
    "accommodation": "住宿",
    "meals": "餐饮",
    "transport": "交通",
    "other": "其他",
}

TYPE_NAMES = {
    "住宿": "hotel",
    "餐饮": "meal",
    "火车": "train",
    "飞机": "flight",
    "出租车": "taxi",
    "其他": "other",
}

SEAT_NAMES = {
    "经济舱": "economy",
    "商务舱": "business",
    "头等舱": "first",
    "二等座": "second",
    "一等座": "first",
    "商务座": "business",
    "硬座": "hard_seat",
    "硬卧": "hard_sleeper",
    "软卧": "soft_sleeper",
}

STATUS_NAMES = STATUS_LABELS


def expense_card(data: dict):
    if data.get("status") == "validation_failed":
        lines = []
        for result in data["results"]:
            title = f"发票 {result['invoice_id']}"
            if result["passed"]:
                lines.append(f"{title}：通过")
            for problem in result["failures"]:
                lines.append(
                    f"{title}：{safe_text(problem['message'])}\n"
                    f"建议：{safe_text(problem['suggestion'])}"
                )
                if problem.get("actual") is not None:
                    lines.append(
                        f"当前值：{safe_text(problem['actual'])}；"
                        f"标准：{safe_text(problem['expected'])}"
                    )

        if data["can_override"]:
            ids = ",".join(map(str, data["invoice_ids"]))
            trip = data["trip_id"] or "无"
            lines.append(
                f"\n如需申请超标报销，请发送：\n"
                f"超标报销 {trip} {ids} 超标原因"
            )

        return card(
            "报销规则校验",
            [{"tag": "markdown", "content": "\n".join(lines)}],
            "orange",
        )

    lines = [
        f"报销单：{safe_text(data['expense_no'])}",
        f"编号：{data['expense_id']}",
        f"状态：{STATUS_NAMES.get(data['status'], data['status'])}",
    ]
    for category, amount in data["breakdown"].items():
        lines.append(
            f"{CATEGORY_NAMES.get(category, category)}：{amount} 元"
        )
    lines.append(f"合计：{data['total_amount']} 元")

    if data.get("has_override"):
        lines.append(
            "包含超标申请，需主管审批。\n"
            f"原因：{safe_text(data.get('override_reason') or '')}"
        )
    if data.get("finance_no"):
        lines.append(f"财务单号：{safe_text(data['finance_no'])}")
    if data.get("last_error"):
        lines.append(safe_text(data["last_error"]))

    elements = [{"tag": "markdown", "content": "\n".join(lines)}]

    if data["status"] == "draft":
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "type": "primary" if operation == "confirm" else "default",
                    "text": {"tag": "plain_text", "content": label},
                    "value": {
                        "module": "expense",
                        "operation": operation,
                        "expense_id": data["expense_id"],
                    },
                }
                for operation, label in (
                    ("confirm", "确认提交"),
                    ("cancel", "取消"),
                )
            ],
        })

    return card("报销单", elements)


async def handle_expense_text(user_id: int, text: str):
    text = text.strip()

    if text in {
        "我的报销单到哪了",
        "我的报销单到哪了？",
        "我的报销单到哪了?",
        "报销进度",
        "查询报销进度",
    }:
        expense_id = await latest_expense_id(user_id)
        if expense_id is None:
            return "暂无已提交的报销单。"
        return query_card(
            await approval_detail(user_id, expense_id)
        )

    match = re.fullmatch(r"查询报销进度\s+(\d+)", text)
    if match:
        return query_card(
            await approval_detail(user_id, int(match[1]))
        )

    match = re.fullmatch(r"重新报销\s+(\d+)", text)
    if match:
        result = await service.reopen_rejected(
            user_id, int(match[1]),
        )
        ids = ",".join(map(str, result["invoice_ids"]))
        trip = result["trip_id"] or "无"
        return (
            result["message"]
            + f"\n原发票ID：{ids}"
            + f"\n修改后可发送：生成报销 {trip} {ids}"
        )

    if text == "我的出差":
        trips = await service.trips(user_id)
        if not trips:
            return "暂无可关联的出差申请。"
        return "\n".join(
            f"{trip['trip_id']}：{trip['destination']}，"
            f"{trip['start_date']} 至 {trip['end_date']}"
            for trip in trips
        )

    match = re.fullmatch(
        r"(查询报销|取消报销)\s+(\d+)",
        text,
    )
    match = re.fullmatch(
        r"(查询报销|取消报销)\s+(\d+)",
        text,
    )
    if match:
        expense_id = int(match[2])
        if match[1] == "查询报销":
            return query_card(
                await approval_detail(user_id, expense_id)
            )
        return expense_card(
            await service.cancel(user_id, expense_id)
        )

    if text.startswith("补充报销"):
        lines = text.strip().splitlines()
        match = re.fullmatch(r"补充报销\s+(\d+)", lines[0])
        if not match:
            raise ExpenseError("格式：补充报销 发票ID，下一行填写费用信息", 422)

        names = {
            "费用类型": "expense_type",
            "城市": "city",
            "入住日期": "check_in",
            "退房日期": "check_out",
            "座席": "seat",
        }
        values = {}
        for line in lines[1:]:
            parts = re.split(r"[:：]", line, maxsplit=1)
            if len(parts) != 2 or parts[0].strip() not in names:
                raise ExpenseError("补充字段名称不正确", 422)
            key = names[parts[0].strip()]
            if key in values:
                raise ExpenseError("同一字段不能重复填写", 422)
            values[key] = parts[1].strip()

        if "expense_type" in values:
            values["expense_type"] = TYPE_NAMES.get(
                values["expense_type"], values["expense_type"],
            )
        if "seat" in values:
            values["seat"] = SEAT_NAMES.get(values["seat"], values["seat"])

        body = ExpenseSupplement.model_validate(values)
        await service.supplement(user_id, int(match[1]), body)
        return "报销信息已补充，可以继续补充其他发票或生成报销单。"

    match = re.fullmatch(
        r"(生成报销|超标报销)\s+(无|\d+)\s+([\d,，]+)(?:\s+(.+))?",
        text,
        flags=re.S,
    )
    if match:
        ids = [
            int(value)
            for value in re.split(r"[,，]", match[3])
        ]
        body = ExpensePrepareInput(
            trip_id=None if match[2] == "无" else int(match[2]),
            invoice_ids=ids,
            accept_overrides=match[1] == "超标报销",
            override_reason=match[4] or "",
        )
        return expense_card(await service.prepare(user_id, body))

    if text.startswith(("生成报销", "超标报销")):
        return (
            "格式：生成报销 出差ID 发票ID列表\n"
            "例如：生成报销 501 101,102\n"
            "非出差报销：生成报销 无 101\n"
            "可以发送“我的出差”查看可用申请。"
        )

    return None