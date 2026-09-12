from app.schemas.attendance import STATUS_VALUES

""""
把考勤数据组织为飞书消息卡片（Interactive Card）格式返回
"""
STATUS_LABELS = {
    alias: aliases[-1]
    for aliases in STATUS_VALUES.values()
    for alias in aliases
}


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

    elements.extend([{"tag": "hr"}, text_block(footer)])

    return {
        "config": {"wide_screen_mode": True},
        "header": {
            "template": "blue",
            "title": {
                "tag": "plain_text",
                "content": f"您的考勤信息 · {result['user_name']}",
            },
        },
        "elements": elements,
    }