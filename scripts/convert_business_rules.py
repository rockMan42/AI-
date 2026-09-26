"""读取显式提供的旧报销 JSON 导出，验证并输出首次发布请求；不访问数据库。"""

import argparse
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.services.business_rules.common import RuleError
from app.services.business_rules.validation import validate_data


def convert(payload):
    from app.schemas.expense import ExpenseRuleInput

    rules, errors = [], []
    for index, item in enumerate(payload["rules"]):
        try:
            value = {
                k: v
                for k, v in item.items()
                if k not in {"id", "created_at", "updated_at"}
            }
            parsed = ExpenseRuleInput.model_validate(value).model_dump(mode="json")
            rules.append({"id": item.get("id", index + 1), **parsed})
        except Exception:
            errors.append(
                {
                    "index": index,
                    "id": item.get("id"),
                    "error": "无法转换，请核对字段和业务约束",
                }
            )
    if errors:
        return {"valid": False, "errors": errors}
    data = validate_data("reimbursement", {"policy": payload["policy"], "rules": rules})
    return {
        "valid": True,
        "request": {
            "expected_version": 0,
            "rule_data": data,
            "bindings": [],
            "change_summary": "从已核对的旧报销配置导入首版",
        },
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path)
    args = parser.parse_args()
    try:
        result = convert(json.loads(args.input.read_text()))
    except (RuleError, ValueError, KeyError) as exc:
        result = {"valid": False, "errors": [{"error": str(exc)}]}
    print(json.dumps(result, ensure_ascii=False, indent=2))
    return 0 if result["valid"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
