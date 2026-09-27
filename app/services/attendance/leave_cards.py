from app.services.attendance.attendance_service import LEAVE_LABELS
from app.services.attendance.feishu_card import text_block


STATUS_LABELS = {
    "pending": "待审批",
    "approved": "已通过 ✅",
    "rejected": "已拒绝",
    "cancelled": "已取消",
}


def button(label: str, operation: str, identifier: str) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "type": "primary" if operation in {"confirm", "approve"} else "default",
        "value": {
            "module": "leave",
            "operation": operation,
            "identifier": identifier,
        },
    }


def card(title: str, elements: list[dict], color: str = "blue") -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def reject_form_card(title: str, value: dict) -> dict:
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": title}},
        "body": {"elements": [{"tag": "form", "name": "approval_reject", "elements": [
            {"tag": "input", "name": "reason",
             "label": {"tag": "plain_text", "content": "驳回原因"}, "required": True},
            {"tag": "button", "name": "submit_reject", "type": "primary",
             "form_action_type": "submit", "text": {"tag": "plain_text", "content": "确认驳回"},
             "behaviors": [{"type": "callback", "value": {**value, "operation": "reject"}}]},
        ]}]},
    }


def summary(data: dict) -> str:
    return (
        f"类型：{LEAVE_LABELS[data['leave_type']]}\n"
        f"日期：{data['start_date']} 至 {data['end_date']}\n"
        f"工作日：{data['duration_days']} 天"
    )


def confirmation_card(draft_id: str, data: dict) -> dict:
    balance_text = ""
    if data.get("remaining_days"):
        balance_text = "\n可用余额：" + "、".join(
            f"{year} 年 {days} 天"
            for year, days in data["remaining_days"].items()
        )
    return card(
        "请确认请假信息",
        [
            text_block(
                summary(data)
                + f"\n理由：{data['reason']}"
                + f"\n审批人：{data['approver_name']}"
                + balance_text
                + "\n确认有效期：30分钟"
            ),
            {
                "tag": "action",
                "actions": [
                    button("确认提交", "confirm", draft_id),
                    button("修改信息", "edit_draft", draft_id),
                    button("取消", "cancel_draft", draft_id),
                ],
            },
        ],
    )


def edit_draft_card(draft_id: str, data: dict) -> dict:
    fields = [
        {"tag": "select_static", "name": "leave_type",
         "placeholder": {"tag": "plain_text", "content": "请选择假别"},
         "initial_option": data["leave_type"],
         "options": [
             {"text": {"tag": "plain_text", "content": label}, "value": code}
             for code, label in LEAVE_LABELS.items()
         ]},
    ]
    for name, label in (("start_date", "开始日期（YYYY-MM-DD）"),
                        ("end_date", "结束日期（YYYY-MM-DD）"),
                        ("reason", "请假理由")):
        fields.append({
            "tag": "input", "name": name,
            "label": {"tag": "plain_text", "content": label},
            "default_value": str(data[name]), "required": True,
        })
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": "修改请假信息"}},
        "body": {"elements": [{"tag": "form", "name": "leave_edit",
            "elements": [*fields, {
                "tag": "button", "name": "save_leave", "type": "primary",
                "form_action_type": "submit",
                "text": {"tag": "plain_text", "content": "保存并预览"},
                "behaviors": [{"type": "callback", "value": {
                    "module": "leave", "operation": "save_draft",
                    "identifier": draft_id,
                }}],
            }]}]},
    }


def request_card(data: dict, *, can_approve: bool = False) -> dict:
    status = data["status"]
    text = (
        summary(data)
        + f"\n状态：{STATUS_LABELS[status]}"
    )

    if data.get("reason"):
        text += f"\n请假理由：{data['reason']}"
    if data.get("reject_reason"):
        text += f"\n拒绝原因：{data['reject_reason']}"

    elements = [text_block(text)]
    if status == "pending":
        actions = (
            [
                button("通过", "approve", data["request_id"]),
                button("拒绝并填写原因", "reject_hint", data["request_id"]),
            ]
            if can_approve
            else [button("撤销申请", "cancel_request", data["request_id"])]
        )
        elements.append({"tag": "action", "actions": actions})

    color = {
        "approved": "green",
        "rejected": "red",
        "cancelled": "grey",
    }.get(status, "blue")
    return card(f"请假申请 · {STATUS_LABELS[status]}", elements, color)


def list_card(items: list[dict], *, inbox: bool, has_more: bool) -> dict:
    elements = []
    for item in items:
        elements.append(text_block(
            f"{LEAVE_LABELS[item['leave_type']]} · "
            f"{item['start_date']} 至 {item['end_date']} · "
            f"{STATUS_LABELS[item['status']]}"
        ))
        elements.append({"tag": "action", "actions": [
            button("查看并处理", "detail", item["request_id"]),
        ]})
    if has_more:
        elements.append(text_block("这里只展示最近的申请，可按日期描述要查询的记录。"))
    return card("待我审批" if inbox else "我的请假", elements)
