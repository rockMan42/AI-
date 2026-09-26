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
    return {
        "header": {
            "template": "blue",
            "title": {"tag": "plain_text", "content": title},
        },
        "elements": [{"tag": "markdown", "content": content}],
    }
