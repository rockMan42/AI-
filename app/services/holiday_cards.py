STATUS = {"draft": "待发布", "published": "已发布", "cancelled": "已取消"}
PUSH = {"PENDING": "待推送", "RUNNING": "推送中", "COMPLETED": "已完成",
        "FAILED": "推送失败", "UNAVAILABLE": "状态暂不可用"}


def notice_list_card(items: list[dict], has_more: bool, *,
                     offset: int = 0, keyword: str | None = None) -> dict:
    elements = []
    for item in items:
        elements.append({"tag": "markdown", "content":
                         f"**{item['title']}**\n"
                         f"{item['start_date']} 至 {item['end_date']} · "
                         f"{STATUS.get(item['status'], '处理中')}"})
        elements.append({"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "查看回执统计"},
            "value": {"module": "holiday", "operation": "stats",
                      "notice_id": item["notice_id"]},
        }]})
    controls = []
    if offset > 0:
        controls.append({"tag": "button", "text": {"tag": "plain_text", "content": "上一页"},
                         "value": {"module": "holiday", "operation": "list",
                                   "offset": max(0, offset - 5), "keyword": keyword}})
    if has_more:
        controls.append({"tag": "button", "text": {"tag": "plain_text", "content": "下一页"},
                         "value": {"module": "holiday", "operation": "list",
                                   "offset": offset + 5, "keyword": keyword}})
    if controls:
        elements.append({"tag": "action", "actions": controls})
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {
            "tag": "plain_text", "content": "假期通知列表"}},
        "elements": elements,
    }


def notice_stats_card(notice: dict, stats: dict) -> dict:
    lines = [
        f"**通知：** {notice['title']}",
        f"**日期：** {notice['start_date']} 至 {notice['end_date']}",
        f"**通知状态：** {STATUS.get(notice['status'], '处理中')}",
        f"**推送状态：** {PUSH.get(notice['push_status'], '处理中')}",
        f"**回执：** 已确认 {stats['confirmed_count']} 人，"
        f"未确认 {stats['unconfirmed_count']} 人，"
        f"确认率 {stats['confirm_rate']}%",
    ]
    departments = stats.get("by_department") or []
    for department in departments[:6]:
        lines.append(
            f"{department['department_name']}："
            f"{department['confirmed']}/{department['total']} 人已确认"
        )
    if len(departments) > 6:
        lines.append("其余部门请在统计报告中查看。")
    actions = [{
        "tag": "button", "text": {"tag": "plain_text", "content": label},
        "value": {"module": "holiday", "operation": operation,
                  "notice_id": notice["notice_id"]},
    } for label, operation in (("刷新统计", "stats"),
                               ("催办未确认人员", "remind"),
                               ("发送统计报告", "report"))]
    return {
        "config": {"wide_screen_mode": True},
        "header": {"template": "blue", "title": {
            "tag": "plain_text", "content": "假期通知回执统计"}},
        "elements": [{"tag": "markdown", "content": "\n".join(lines)},
                     {"tag": "action", "actions": actions}],
    }
