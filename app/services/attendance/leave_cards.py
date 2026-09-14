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


def summary(data: dict) -> str:
    return (
        f"类型：{LEAVE_LABELS[data['leave_type']]}\n"
        f"日期：{data['start_date']} 至 {data['end_date']}\n"
        f"工作日：{data['duration_days']} 天"
    )


def confirmation_card(draft_id: str, data: dict) -> dict:
    return card(
        "请确认请假信息",
        [
            text_block(
                summary(data)
                + f"\n理由：{data['reason']}"
                + f"\n审批人：{data['approver_name']}"
                + "\n确认有效期：30分钟"
            ),
            {
                "tag": "action",
                "actions": [
                    button("确认提交", "confirm", draft_id),
                    button("取消", "cancel_draft", draft_id),
                ],
            },
        ],
    )


def request_card(data: dict, *, can_approve: bool = False) -> dict:
    status = data["status"]
    text = (
        f"申请编号：{data['request_id']}\n"
        + summary(data)
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