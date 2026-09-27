from app.schemas.attendance import STATUS_VALUES

""""
把考勤数据组织为飞书消息卡片（Interactive Card）格式返回
"""
STATUS_LABELS = {
    alias: aliases[-1]
    for aliases in STATUS_VALUES.values()
    for alias in aliases
}


STATUS_LABELS.update({"needs_review": "待核实", "rest": "休息日", "pending": "班次未结束"})


def text_block(content: str) -> dict:
    return {
        "tag": "div",
        "text": {
            "tag": "plain_text",
            "content": content,
        },
    }


def build_attendance_card(result: dict) -> dict:
    elements = []
    month = result["month"]

    stats = result.get("late_stats")
    if stats is not None:
        if stats["record_count"] == 0:
            elements.append(text_block(f"{month} 暂无考勤统计数据"))
        else:
            elements.append(
                {
                    "tag": "div",
                    "fields": [
                        {
                            "is_short": True,
                            "text": {
                                "tag": "plain_text",
                                "content": (
                                    f"{month} 迟到\n"
                                    f"{stats['late_count']} 次"
                                ),
                            },
                        },
                        {
                            "is_short": True,
                            "text": {
                                "tag": "plain_text",
                                "content": (
                                    f"{month} 早退\n"
                                    f"{stats['early_leave_count']} 次"
                                ),
                            },
                        },
                    ],
                }
            )

    records = result.get("punch_records")
    if records is not None:
        scope = result["query_date"] or month
        lines = [f"{scope} 打卡记录（最近 5 条）"]

        if result["status_filter"]:
            lines.append(
                f"状态筛选：{STATUS_LABELS[result['status_filter']]}"
            )

        for row in records["items"]:
            status = STATUS_LABELS.get(row["status"], row["status"])
            if "late" in row.get("issues", []) and "early_leave" in row.get("issues", []):
                status = "迟到且早退"
            lines.append(
                f"{row['date']}  "
                f"上班 {row['punch_in'] or '未打卡'} | "
                f"下班 {row['punch_out'] or '未打卡'} | "
                f"{status}"
            )

        if not records["items"]:
            lines.append("暂无符合条件的打卡记录")

        elements.append(text_block("\n".join(lines)))

    balances = result.get("leave_balances")
    if balances is not None:
        lines = [f"{result['leave_year']} 年假期余额"]
        for item in balances:
            if item["remaining_days"] is None:
                lines.append(f"{item['label']}：暂无数据")
            else:
                lines.append(
                    f"{item['label']}：剩余 {item['remaining_days']} 天"
                    f"（共 {item['total_days']} 天，"
                    f"已用 {item['used_days']} 天）"
                )

        elements.append(text_block("\n".join(lines)))

    footer = (
        f"数据来源：{result['source']}\n"
        f"查询时间：{result['queried_at'][:10]}"
    )
    if stats is not None:
        footer += f"\n统计生成时间：{stats['calculated_at'][:10]}"
        if stats["cached"]:
            footer += "（缓存，最长 60 秒）"

    if result.get("rule_version") is not None:
        footer += f"\n按当前考勤规则 v{result['rule_version']} 重算，历史月份结果可能变化"

    elements.extend([{"tag": "hr"}, text_block(footer)])
    elements.append({"tag": "action", "actions": [{
        "tag": "button",
        "text": {"tag": "plain_text", "content": "筛选月份、日期或异常"},
        "value": {"module": "attendance", "operation": "filter",
                  "target_user_id": result["user_id"],
                  "query_type": result["query_type"]},
    }]})

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"考勤信息 · {result['user_name']}",
            },
        },
        "elements": elements,
    }


def build_attendance_card_v2(result: dict) -> dict:
    """表单提交后返回同版本卡片，保留概览、余额及再次筛选入口。"""
    legacy = build_attendance_card(result)
    elements = []
    for item in legacy["elements"]:
        if item["tag"] == "div" and "fields" in item:
            elements.extend({"tag": "div", "text": field["text"]}
                            for field in item["fields"])
        elif item["tag"] == "action":
            elements.extend({
                "tag": "button", "text": button["text"],
                "behaviors": [{"type": "callback", "value": button["value"]}],
            } for button in item["actions"])
        elif item["tag"] != "hr":
            elements.append(item)
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content":
            f"{'假期余额' if result['query_type'] == 'leave_balance' else '考勤信息'}"
            f" · {result['user_name']}"}},
        "body": {"elements": elements},
    }


def attendance_filter_card(
    target_user_id: int, user_name: str, month: str,
    query_type: str = "attendance",
) -> dict:
    if query_type not in {"attendance", "punch_record", "late_count", "leave_balance"}:
        query_type = "attendance"
    fields = [{
        "tag": "select_static", "name": "query_type",
        "placeholder": {"tag": "plain_text", "content": "选择查询内容"},
        "initial_option": query_type,
        "options": [
            {"text": {"tag": "plain_text", "content": label}, "value": code}
            for code, label in (("attendance", "考勤概览"),
                                ("punch_record", "打卡记录"),
                                ("late_count", "迟到早退统计"),
                                ("leave_balance", "假期余额"))
        ],
    }]
    for name, label, initial in (
        ("month", "月份（YYYY-MM）", month),
        ("query_date", "指定日期（可选，YYYY-MM-DD）", ""),
        ("target_name", "员工姓名（可选，仅限有权限查询的员工）", ""),
    ):
        fields.append({"tag": "input", "name": name, "required": False,
                       "label": {"tag": "plain_text", "content": label},
                       "default_value": initial})
    fields.append({
        "tag": "select_static", "name": "status_filter",
        "placeholder": {"tag": "plain_text", "content": "筛选异常（可选）"},
        "options": [
            {"text": {"tag": "plain_text", "content": label}, "value": code}
            for code, label in (("late", "迟到"), ("early_leave", "早退"),
                                ("absent", "缺勤"), ("leave", "请假"),
                                ("normal", "正常"))
        ],
    })
    return {
        "schema": "2.0", "config": {"width_mode": "fill"},
        "header": {"title": {"tag": "plain_text", "content": f"筛选考勤 · {user_name}"}},
        "body": {"elements": [{"tag": "form", "name": "attendance_filter",
            "elements": [*fields, {
                "tag": "button", "name": "submit_filter", "type": "primary",
                "form_action_type": "submit",
                "text": {"tag": "plain_text", "content": "查询考勤"},
                "behaviors": [{"type": "callback", "value": {
                    "module": "attendance", "operation": "query",
                    "target_user_id": target_user_id,
                }}],
            }]}]},
    }
