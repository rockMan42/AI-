def requisition_confirmation_card(draft: dict) -> dict:
    fields = [
        f"**品类**：{draft['item_category']}",
        f"**物资名称**：{draft['item_name']}",
        f"**数量**：{draft['quantity']}",
    ]

    optional_fields = (
        ("specification", "规格型号"),
        ("reason", "申领原因"),
        ("purpose", "用途说明"),
        ("expected_return_date", "预计归还日期"),
    )

    for key, label in optional_fields:
        if draft.get(key):
            fields.append(f"**{label}**：{draft[key]}")

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": "📦 物资申领确认",
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": "\n".join(fields),
            },
            {
                "tag": "action",
                "actions": [
                    {
                        "tag": "button",
                        "type": "primary",
                        "text": {
                            "tag": "plain_text",
                            "content": "确认提交",
                        },
                        "value": {
                            "module": "requisition",
                            "operation": "confirm",
                            "draft_id": draft["draft_id"],
                        },
                    },
                    {
                        "tag": "button",
                        "type": "danger",
                        "text": {
                            "tag": "plain_text",
                            "content": "取消",
                        },
                        "value": {
                            "module": "requisition",
                            "operation": "cancel",
                            "draft_id": draft["draft_id"],
                        },
                    },
                ],
            },
        ],
    }


def requisition_result_card(
    message: str,
    *,
    cancelled: bool = False,
) -> dict:
    """确认或取消后替换原卡片，不再保留可重复点击的按钮。"""
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "grey" if cancelled else "green",
            "title": {
                "tag": "plain_text",
                "content": (
                    "物资申领已取消"
                    if cancelled
                    else "物资申领已提交"
                ),
            },
        },
        "elements": [
            {
                "tag": "markdown",
                "content": message,
            },
        ],
    }
