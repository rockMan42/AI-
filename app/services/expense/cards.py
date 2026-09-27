from html import escape
import re

from app.schemas.invoice import FIELD_LABELS


def safe_text(value) -> str:
    return re.sub(
        r"([\\*`_\[\]])",
        r"\\\1",
        escape(str(value)),
    )


def card(
    title: str,
    elements: list[dict],
    color="blue",
) -> dict:
    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": color,
            "title": {
                "tag": "plain_text",
                "content": title,
            },
        },
        "elements": elements,
    }


def invoice_card(
    request_id: str,
    item: dict,
    editing=False,
) -> dict:
    low_fields = set(item.get("low_confidence_fields", []))
    lines = []

    for name, label in FIELD_LABELS.items():
        value = item.get(name)
        displayed = (
            safe_text(value)
            if value is not None
            else "未识别/不适用"
        )
        line = f"{label}：{displayed}"

        if name in low_fields:
            line = (
                f"<font color='red'>{line}"
                "（请确认）</font>"
            )

        lines.append(line)

    if item.get("error"):
        lines.append(safe_text(item["error"]))

    elements = [
        {
            "tag": "markdown",
            "content": "\n".join(lines),
        }
    ]
    verified = item.get("verified", False)

    if editing:
        fields = [
            {
                "tag": "input",
                "name": name,
                "label": {"tag": "plain_text", "content": label},
                "default_value": str(item.get(name) if item.get(name) is not None else ""),
                "placeholder": {"tag": "plain_text", "content": "无法确定时填“不适用”"},
                "required": False,
            }
            for name, label in FIELD_LABELS.items()
        ]
        return {
            "schema": "2.0",
            "config": {"width_mode": "fill"},
            "header": {
                "template": "blue",
                "title": {"tag": "plain_text", "content": f"核对第 {item['index']} 张发票"},
            },
            "body": {"elements": [
                {"tag": "div", "text": {"tag": "plain_text", "content": "只修改有误的内容；保存后即确认这张发票。"}},
                {"tag": "form", "name": "invoice_edit", "elements": [
                    *fields,
                    {
                        "tag": "button", "name": "save_invoice", "type": "primary",
                        "form_action_type": "submit",
                        "text": {"tag": "plain_text", "content": "保存并确认"},
                        "behaviors": [{"type": "callback", "value": {
                            "module": "invoice", "operation": "save_changes",
                            "request_id": request_id, "index": item["index"],
                        }}],
                    },
                ]},
            ]},
        }

    if not verified and item.get("invoice_id"):
        elements.append({
            "tag": "action",
            "actions": [
                {
                    "tag": "button",
                    "type": (
                        "primary"
                        if operation == "confirm"
                        else "default"
                    ),
                    "text": {
                        "tag": "plain_text",
                        "content": label,
                    },
                    "value": {
                        "module": "invoice",
                        "operation": operation,
                        "request_id": request_id,
                        "index": item["index"],
                    },
                }
                for operation, label in (
                    ("confirm", "确认正确"),
                    ("modify", "需要修改"),
                )
            ],
        })

    return card(
        f"发票 {item['index']} · "
        f"{'已确认' if verified else '识别结果'}",
        elements,
        "green" if verified else "blue",
    )


def summary_card(
    batch: dict,
    upload_failed=0,
) -> dict:
    summary = batch["summary"]
    total = summary["total_count"] + upload_failed

    result = card(
        "发票识别汇总",
        [
            {
                "tag": "div",
                "text": {
                    "tag": "plain_text",
                    "content": (
                        f"共收到 {total} 张图片\n"
                        f"已处理 {summary['total_count']} 张\n"
                        "低置信度或识别失败 "
                        f"{summary['needs_confirm_count']} 张\n"
                        f"已人工确认 {summary['confirmed_count']} 张\n"
                        f"图片接收失败 {upload_failed} 张"
                    ),
                },
            }
        ],
    )
    result["config"]["update_multi"] = True

    invoice_ids = [
        str(item["invoice_id"])
        for item in batch.get("results", [])
        if item.get("invoice_id")
    ]

    if invoice_ids:
        result["elements"].append({
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": (
                    "请逐张核对识别结果。全部确认后，"
                    "系统会继续引导您选择出差申请或补充费用信息。"
                ),
            },
        })

    return result
