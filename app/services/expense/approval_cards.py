from app.services.expense.approval_state import (
    REJECTED_STATUSES,
    STATUS_LABELS,
    rejection_suggestion,
)
from app.services.expense.cards import card, safe_text
from app.utils.time import format_shanghai


def status_card(expense, event) -> dict:
    status = event.to_status
    lines = [
        f"报销单号：{safe_text(expense.expense_no)}",
        f"报销金额：¥{expense.total_amount}",
        f"审批节点：{STATUS_LABELS.get(status, status)}",
        f"发生时间：{format_shanghai(event.occurred_at)}",
    ]
    title = "报销审批进度更新"
    color = "green"

    if event.approver_name:
        lines.append(f"审批人：{safe_text(event.approver_name)}")

    if status in REJECTED_STATUSES:
        title = "报销单被退回"
        color = "red"
        reason = event.comment or "未说明原因"
        lines.extend([
            f"退回原因：{safe_text(reason)}",
            f"修改建议：{safe_text(rejection_suggestion(reason))}",
            "点击下方按钮开放修改；修改后需要重新生成并确认提交。",
        ])
    elif status == "paid":
        title = "报销款已到账"
        color = "blue"
        lines.extend([
            f"到账金额：¥{safe_text(event.detail['paid_amount'])}",
            f"打款时间：{safe_text(event.detail['paid_at'])}",
        ])
    elif event.comment:
        lines.append(f"审批意见：{safe_text(event.comment)}")

    elements = [{"tag": "markdown", "content": "\n".join(lines)}]

    if status in REJECTED_STATUSES:
        elements.append({
            "tag": "action",
            "actions": [{
                "tag": "button",
                "type": "primary",
                "text": {"tag": "plain_text", "content": "重新提交"},
                "value": {
                    "module": "expense",
                    "operation": "resubmit",
                    "expense_id": expense.id,
                },
            }],
        })

    return card(title, elements, color)


def timeout_card(items: list[dict]) -> dict:
    lines = ["以下报销单在当前节点停留超过 3 个工作日："]
    for item in items:
        lines.append(
            f"\n报销单：{safe_text(item['expense_no'])}\n"
            f"金额：¥{safe_text(item['total_amount'])}\n"
            f"节点：{STATUS_LABELS[item['status']]}\n"
            f"停留：{item['working_days']} 个工作日"
        )
    return card(
        "报销审批超时提醒",
        [{"tag": "markdown", "content": "\n".join(lines)}],
        "yellow",
    )


def query_card(data: dict) -> dict:
    lines = [
        f"报销单：{safe_text(data['expense_no'])}",
        f"当前状态：{data['status_label']}",
        f"金额：¥{data['total_amount']}",
        f"最近同步：{data['synced_at'] or '尚未同步'}",
    ]
    for item in data["approval_log"]:
        lines.append(
            f"\n{item['occurred_at']}\n"
            f"{STATUS_LABELS.get(item['from_status'], item['from_status'])}"
            f" → {STATUS_LABELS.get(item['to_status'], item['to_status'])}\n"
            f"审批人：{safe_text(item['approver_name'] or '系统')}\n"
            f"意见：{safe_text(item['comment'] or '无')}"
        )
    return card(
        "报销审批记录",
        [{"tag": "markdown", "content": "\n".join(lines)}],
    )
