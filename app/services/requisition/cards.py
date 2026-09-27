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

    if draft.get("required_fields"):
        fields.append("**本品类必填项**：" + "、".join(draft["required_fields"]))
    fields.append("**审批安排**：提交后由 OA 按现有规则分配，并在状态卡片中展示进度")

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
                        "text": {"tag": "plain_text", "content": "修改信息"},
                        "value": {"module": "requisition", "operation": "edit",
                                  "draft_id": draft["draft_id"]},
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


def requisition_edit_card(draft: dict) -> dict:
    fields = []
    for name, label, required in (
        ("item_category", "品类", True),
        ("item_name", "物资名称", True),
        ("specification", "规格型号", False),
        ("quantity", "数量", True),
        ("reason", "申领原因", False),
        ("purpose", "用途说明", False),
        ("expected_return_date", "预计归还日期（YYYY-MM-DD）", False),
    ):
        fields.append({
            "tag": "input", "name": name,
            "label": {"tag": "plain_text", "content": label},
            "default_value": str(draft.get(name) or ""),
            "required": required,
        })
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": "修改物资申领"}},
        "body": {"elements": [{"tag": "form", "name": "requisition_edit",
            "elements": [*fields, {
                "tag": "button", "name": "save_requisition", "type": "primary",
                "form_action_type": "submit",
                "text": {"tag": "plain_text", "content": "保存并预览"},
                "behaviors": [{"type": "callback", "value": {
                    "module": "requisition", "operation": "save",
                    "draft_id": draft["draft_id"],
                }}],
            }]}]},
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
