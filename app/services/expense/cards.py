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
    if item.get("invoice_id"):
        lines.append(f"发票ID：{item['invoice_id']}")

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
                    f"本批发票ID：{','.join(invoice_ids)}\n"
                    "全部确认后，系统会自动识别费用信息并推荐出差申请。\n"
                    "只有无法自动识别时才需要手动补充。\n\n"
                    "备用操作示例：\n"
                    f"补充报销 {invoice_ids[0]}\n"
                    "费用类型：住宿\n"
                    "城市：上海\n"
                    "入住日期：2026-09-10\n"
                    "退房日期：2026-09-12\n\n"
                    "餐饮只需填写费用类型；火车、飞机还需填写座席。\n"
                    "补充完成后可发送：生成报销 出差ID 发票ID列表\n"
                    "例如：生成报销 501 101,102\n"
                    "可以跨批次选择发票；不需要报销的发票不选即可。"
                ),
            },
        })

    return result
