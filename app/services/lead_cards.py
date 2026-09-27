from datetime import datetime

from app.schemas.lead import STATUS_LABELS
from app.services.expense.cards import card, safe_text
from app.utils.time import format_shanghai


def format_card_date(value: str | datetime | None, empty: str = "未填写") -> str:
    if not value:
        return empty
    if isinstance(value, str):
        value = datetime.fromisoformat(value.replace("Z", "+00:00"))
    return format_shanghai(value, "%Y年%m月%d日")


def markdown(content: str) -> dict:
    return {"tag": "markdown", "content": content}


def button(label: str, operation: str, **values) -> dict:
    return {
        "tag": "button",
        "text": {"tag": "plain_text", "content": label},
        "value": {
            "module": "lead",
            "operation": operation,
            **values,
        },
    }


def actions(*buttons) -> dict:
    return {"tag": "action", "actions": list(buttons)}


def lead_line(item: dict) -> str:
    labels = []
    if item["is_high_priority"]:
        labels.append("<font color='red'>高</font>")
    if item["is_overdue"]:
        labels.append(
            f"<font color='orange'>超期 {item['overdue_days']} 天</font>"
        )
    if item["never_followed"]:
        labels.append("尚未跟进")

    return (
        f"{' '.join(labels)} **{safe_text(item['company_name'])}**"
        f"｜{STATUS_LABELS[item['status']]}"
        f"｜优先级：{ {'high': '高', 'medium': '中', 'low': '低'}.get(item['priority'], '普通')}"
    )


def priority_reason(item: dict) -> str:
    reasons = []
    parts = item.get("score_breakdown") or {}
    if parts.get("budget_score", 0) >= 80:
        reasons.append("预算较高")
    if parts.get("interaction_score", 0) >= 60:
        reasons.append("近30天互动活跃")
    if parts.get("time_decay_score", 0) >= 80:
        reasons.append("近期有跟进")
    if item.get("is_overdue"):
        reasons.append("超过7天未跟进")
    return "、".join(reasons) or "按预算、互动和跟进时间综合排序"


def follow_up_form(lead_id: int, company_name: str) -> dict:
    choices = (
        ("phone", "电话"), ("email", "邮件"), ("visit", "拜访"),
        ("wechat", "微信"), ("demo", "演示"),
    )
    outcomes = (
        ("positive", "积极"), ("neutral", "一般"),
        ("negative", "消极"), ("no_answer", "未接通"),
    )
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": f"记录跟进 · {company_name}"}},
        "body": {"elements": [{"tag": "form", "name": "lead_follow_up", "elements": [
            {"tag": "select_static", "name": "follow_up_type",
             "placeholder": {"tag": "plain_text", "content": "选择跟进方式"},
             "options": [{"text": {"tag": "plain_text", "content": label}, "value": value}
                         for value, label in choices]},
            {"tag": "input", "name": "content", "required": True,
             "label": {"tag": "plain_text", "content": "本次跟进内容"}},
            {"tag": "select_static", "name": "outcome",
             "placeholder": {"tag": "plain_text", "content": "选择跟进结果（可选）"},
             "options": [{"text": {"tag": "plain_text", "content": label}, "value": value}
                         for value, label in outcomes]},
            {"tag": "input", "name": "next_action", "required": False,
             "label": {"tag": "plain_text", "content": "下一步安排（可选）"}},
            {"tag": "input", "name": "next_follow_up", "required": False,
             "label": {"tag": "plain_text", "content": "下次跟进时间（可选）"},
             "placeholder": {"tag": "plain_text", "content": "例如 2026-09-30 09:00"}},
            {"tag": "button", "name": "save_follow_up", "type": "primary",
             "form_action_type": "submit",
             "text": {"tag": "plain_text", "content": "保存跟进记录"},
             "behaviors": [{"type": "callback", "value": {
                 "module": "lead", "operation": "follow_up_save", "lead_id": lead_id,
             }}]},
        ]}]},
    }


def follow_up_saved_card(company_name: str, content: str) -> dict:
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": "跟进记录已保存"}},
        "body": {"elements": [{"tag": "markdown", "content":
            f"**{safe_text(company_name)}**\n本次跟进：{safe_text(content[:200])}\n"
            "需要继续查看时，可重新查询线索。"}]},
    }


def query_card(data: dict, params: dict) -> dict:
    statistics = " · ".join(
        f"{label} {data['statistics'].get(status, 0)}"
        for status, label in STATUS_LABELS.items()
    )
    elements = [markdown(statistics)]

    for item in data["leads"]:
        elements.extend([
            markdown(
                lead_line(item)
                + f"\n联系人：{safe_text(item['contact_name'])}"
                + f"｜负责人：{safe_text(item['assigned_name'] or '未分配')}"
                + f"\n排序参考：{priority_reason(item)}"
            ),
            actions(
                button("查看详情", "detail", lead_id=item["id"]),
                button("记录跟进", "follow_up", lead_id=item["id"]),
            ),
        ])

    if not data["leads"]:
        elements.append(markdown("暂无符合条件的线索。"))

    if data["page"] * data["page_size"] < data["total"]:
        elements.append(actions(button(
            "查看更多",
            "query",
            params={**params, "page": data["page"] + 1},
        )))

    return card(
        f"客户线索 · 共 {data['total']} 条 · 第 {data['page']} 页",
        elements,
    )


def detail_card(data: dict, history_page: int = 1) -> dict:
    labels = {
        "contact_name": "联系人",
        "contact_phone": "电话",
        "contact_email": "邮箱",
        "budget": "预算（万元）",
        "source": "来源",
        "assigned_name": "负责人",
        "remark": "备注",
        "last_follow_up": "最近跟进",
        "next_follow_up": "下次跟进",
        "created_at": "创建时间",
        "updated_at": "更新时间",
    }
    date_fields = {"last_follow_up", "next_follow_up", "created_at", "updated_at"}
    elements = [markdown(lead_line(data))]
    lines = []
    for name, label in labels.items():
        value = data.get(name)
        if name in date_fields:
            value = format_card_date(value)
        elif value is None:
            value = "未填写"
        lines.append(f"{label}：{safe_text(value)}")
    elements.append(markdown("\n".join(lines)))

    records = data["follow_ups"]
    start = (history_page - 1) * 5
    for record in records[start:start + 5]:
        elements.append(markdown(
            f"**{format_card_date(record['created_at'])}**"
            f"｜{safe_text(record['user_name'])}"
            f"｜{safe_text(record['follow_up_type'])}\n"
            f"{safe_text(record['content'])}\n"
            f"结果：{safe_text(record['outcome'] or '未填写')}\n"
            f"下一步：{safe_text(record['next_action'] or '未填写')}\n"
            f"下次跟进：{format_card_date(record['next_follow_up'], '未安排')}"
        ))

    if not records:
        elements.append(markdown("暂无跟进记录。"))

    controls = [
        button("记录跟进", "follow_up", lead_id=data["id"]),
    ]
    if start + 5 < len(records):
        controls.append(button(
            "更多跟进记录",
            "detail",
            lead_id=data["id"],
            history_page=history_page + 1,
        ))
    elements.append(actions(*controls))
    return card("线索详情", elements)


def reminder_card(items: list[dict], mode: str) -> dict:
    types = {
        alert_type
        for item in items
        for alert_type in item["alert_types"]
    }
    color = (
        "blue" if mode == "due_soon"
        else "orange" if "overdue" in types
        else "red"
    )
    elements = []

    for item in items[:10]:
        messages = []
        if "overdue" in item["alert_types"]:
            messages.append(f"超过 7 天未跟进，已间隔 {item['overdue_days']} 天")
        if "high_intent" in item["alert_types"]:
            messages.append("高意向客户，建议电话确认下一步安排")
        if "due_soon" in item["alert_types"]:
            messages.append(f"计划跟进：{format_card_date(item['next_follow_up'], '未安排')}")

        elements.extend([
            markdown(
                lead_line(item)
                + "\n" + "；".join(messages)
                + "\n最近跟进摘要："
                + safe_text(item.get("latest_content") or "暂无跟进记录")
            ),
            actions(button(
                "记录跟进", "follow_up", lead_id=item["id"],
            )),
        ])

    if len(items) > 10:
        elements.append(markdown("本卡展示前 10 条，其余线索可继续查看。"))
        labels = {
            "overdue": "查看超期线索",
            "high_intent": "查看高意向线索",
            "due_soon": "查看到期线索",
        }
        elements.append(actions(*[
            button(
                labels[view],
                "query",
                params={"view": view, "sort_by": "priority_score"},
            )
            for view in sorted(types)
        ]))

    return card(
        f"您有 {len(items)} 条线索需要关注",
        elements,
        color,
    )
