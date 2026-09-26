"""识别明确的业务规则操作，避免误送知识库或等待模型超时。"""
import re


_RULE_QUERY = re.compile(
    r"^(?:请|麻烦)?(?:帮我|给我)?"
    r"(?:(?:查|查看|查询|看一下|看下|展示|显示)(?:一下)?)?"
    r"(?:当前|最新|现行)?(?:的)?"
    r"(?P<rule_type>考勤|报销|审批)(?:业务|配置|执行)?规则"
    r"(?:是什么|有哪些|内容|配置)?(?:[。！？!?])?$"
)

_RULE_ACTION = re.compile(
    r"^(?:请|麻烦)?(?:帮我|给我)?"
    r"(?P<verb>编辑|修改|调整|查看|查询|对比|比较)"
    r"(?P<rule_type>考勤|报销|审批)(?:业务|配置|执行)?规则"
    r"(?P<tail>的?历史版本|的?版本历史|的?第一版和第二版|的?第\d+版和第\d+版)?"
    r"(?:[。！？!?])?$"
)
_VERSIONS = re.compile(r"第(\d+|一|二)版和第(\d+|一|二)版")
_RULE_EDIT = re.compile(
    r"^(?:请|麻烦)?(?:帮我|给我)?(?:编辑|修改|调整)"
    r"(?P<rule_type>考勤|报销|审批)(?:业务|配置|执行)?规则"
    r"(?:的.{1,100})?(?:[。！？!?])?$"
)


def parse_business_rule_query(text: str) -> dict | None:
    normalized = text.strip()
    match = (_RULE_ACTION.fullmatch(normalized) or _RULE_QUERY.fullmatch(normalized)
             or _RULE_EDIT.fullmatch(normalized))
    if match is None:
        return None
    groups = match.groupdict()
    verb = groups.get("verb")
    tail = groups.get("tail") or ""
    operation = (
        "edit" if match.re is _RULE_EDIT or verb in {"编辑", "修改", "调整"}
        else "versions" if "历史" in tail
        else "compare" if verb in {"对比", "比较"}
        else "view"
    )
    slots = {
        "rule_type": {
            "考勤": "attendance",
            "报销": "reimbursement",
            "审批": "approval",
        }[match["rule_type"]],
        "operation": operation,
    }
    versions = _VERSIONS.search(tail)
    if versions and operation == "compare":
        numbers = {"一": "1", "二": "2"}
        slots.update(from_version=numbers.get(versions[1], versions[1]),
                     version=numbers.get(versions[2], versions[2]))
    return {
        "intent": "business_rule_manage",
        "confidence": 1.0,
        "extracted_slots": slots,
    }
