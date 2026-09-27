def performance_card(title: str, data: dict) -> dict:
    if "score" in data:
        content = (
            f"**周期：** {data['cycle']}\n"
            f"**评分：** {data['score']}\n"
            f"**等级：** {data['grade']}\n"
            f"**部门百分位：** {data['department_percentile']}%\n"
            f"**评语：** {data['comment'] or '暂无'}"
        )
    else:
        summary = data.get("summary") or {}
        content = (
            f"**周期：** {data['cycle']}\n"
            f"**人数：** {summary.get('total_count', 0)}\n"
            f"**平均分：** {summary.get('avg_score', 0)}\n"
            f"**标准差：** {summary.get('std_dev', 0)}\n"
            f"**异常数：** {len(data.get('anomalies') or data.get('alerts') or [])}"
        )
        distribution = data.get("distribution") or []
        if distribution:
            content += "\n\n**分数分布**\n" + "\n".join(
                f"{item['range']}：{item['count']} 人"
                for item in distribution
            )
        if data.get("departments"):
            content += "\n\n**部门概览**\n" + "\n".join(
                f"{item['department']}：{item['total_count']} 人，"
                f"平均 {item['avg_score']} 分"
                for item in data["departments"][:5]
            )
    elements = [{"tag": "markdown", "content": content}]
    if "score" not in data:
        elements.append({"tag": "action", "actions": [{
            "tag": "button", "text": {"tag": "plain_text", "content": "查看统计报告"},
            "value": {"module": "performance", "operation": "report",
                      "cycle": data["cycle"], "grade": data.get("grade_filter")},
        }]})
    return {
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": elements,
    }


def performance_report_card(title: str, data: dict, page: int = 1) -> dict:
    card = performance_card(title, data)
    card["elements"] = card["elements"][:1]
    total = len(data.get("members") or data.get("departments") or [])
    if total and (page - 1) * 15 >= total:
        page = (total - 1) // 15 + 1
    start = (page - 1) * 15
    if data.get("members"):
        card["elements"].append({"tag": "markdown", "content":
            f"**成员概览 · 第 {page} 页**\n" + "\n".join(
                f"{item['name']}：{item['score']} 分 · {item['grade']}"
                for item in data["members"][start:start + 15]
            )})
    if data.get("departments"):
        card["elements"].append({"tag": "markdown", "content":
            f"**部门统计 · 第 {page} 页**\n" + "\n".join(
                f"{item['department']}：{item['total_count']} 人，"
                f"平均 {item['avg_score']} 分"
                for item in data["departments"][start:start + 15]
            )})
    alerts = data.get("anomalies") or data.get("alerts") or []
    if alerts:
        card["elements"].append({"tag": "markdown", "content":
            f"异常提示：{len(alerts)} 项，请在管理端核对明细。"})
    base = {"module": "performance", "operation": "report",
            "cycle": data["cycle"], "grade": data.get("grade_filter")}
    buttons = []
    if page > 1:
        buttons.append({"tag": "button", "text": {"tag": "plain_text", "content": "上一页"},
                        "value": {**base, "page": page - 1}})
    if page * 15 < total:
        buttons.append({"tag": "button", "text": {"tag": "plain_text", "content": "下一页"},
                        "value": {**base, "page": page + 1}})
    buttons.append({"tag": "button", "text": {"tag": "plain_text", "content": "返回摘要"},
                    "value": {**base, "operation": "summary"}})
    card["elements"].append({"tag": "action", "actions": buttons})
    return card
