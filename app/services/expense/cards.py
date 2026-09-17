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
        elements.append({
            "tag": "div",
            "text": {
                "tag": "plain_text",
                "content": (
                    "请私聊发送以下格式，只填写需要修改的行：\n"
                    f"修改发票 {request_id} {item['index']}\n"
                    "购买方名称：正确的公司名称\n"
                    "价税合计：358.00\n"
                    "开票日期：2026-08-15\n"
                    "其他字段使用上面展示的中文名称；"
                    "不适用填“不适用”。\n"
                    "发送即表示确认修改后的结果。"
                ),
            },
        })

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
    return result
