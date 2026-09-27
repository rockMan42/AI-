def parse_form_values(raw: object, allowed: set[str]) -> dict[str, str]:
    """读取飞书表单字段；下拉控件兼容字符串及带 value 的对象。"""
    if not isinstance(raw, dict):
        raise ValueError("卡片内容格式不正确，请重新打开")
    result = {}
    for name, value in raw.items():
        # 飞书会把提交按钮的 name 一并放进 form_value，值为 null。
        if name not in allowed:
            if value is None:
                continue
            raise ValueError("卡片内容格式不正确，请重新打开")
        if isinstance(value, dict):
            value = value.get("value")
        if not isinstance(value, str):
            raise ValueError("卡片内容格式不正确，请重新填写")
        result[name] = value.strip()
    return result
