"""运行 07i02 本机隔离验收，并生成逐项报告。

用法：.venv/bin/python scripts/notification_acceptance.py
测试只在本机数据库中新建随机库，Redis 使用随机命名空间。
"""

import json
import os
import subprocess
import sys
import xml.etree.ElementTree as ET
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from app.config.settings import get_settings


CHECKS = {
    "审批催办": ["test_four_scenes_collect_correct_recipients", "test_scene_boundaries_and_stale_approval",
               "test_requisition_callback_duplicate_and_forged_signature", "test_expense_mock_approval_current_node"],
    "出勤异常": ["test_four_scenes_collect_correct_recipients", "test_attendance_card_update_and_summary_limit"],
    "线索跟进": ["test_four_scenes_collect_correct_recipients", "test_scene_boundaries_and_stale_approval"],
    "考核截止": ["test_four_scenes_collect_correct_recipients", "test_scene_boundaries_and_stale_approval"],
    "幂等与合并": ["test_merge_and_deduplicate", "test_parallel_worker_retry_exhaustion_and_recovery"],
    "失败重试": ["test_retry_and_daily_limit", "test_parallel_worker_retry_exhaustion_and_recovery",
                 "test_response_lost_retry_reuses_message_uuid", "test_feishu_http_simulator_delivery"],
    "卡片交互": ["test_requisition_callback_duplicate_and_forged_signature", "test_rejection_form_requires_reason",
                 "test_expense_mock_approval_current_node"],
    "每日上限": ["test_retry_and_daily_limit", "test_attendance_card_update_and_summary_limit"],
    "模板与管理": ["test_hr_template_schedule_and_test_api", "test_hermes_schedule_uses_isolated_store"],
    "通知查询": ["test_notification_http_owner_and_hr"],
}


def main() -> int:
    settings = get_settings()
    database = settings.database_url
    redis_url = settings.redis_url
    for label, address in (("MySQL", database), ("Redis", redis_url)):
        if urlsplit(address).hostname not in {"localhost", "127.0.0.1"}:
            raise SystemExit(f"{label} 必须是本机地址；拒绝运行隔离验收")

    run_id = datetime.now().strftime("%Y%m%d_%H%M%S") + "_" + uuid4().hex[:6]
    output = ROOT / "artifacts" / f"notification_acceptance_{run_id}"
    output.mkdir(parents=True, exist_ok=False)
    env = os.environ.copy()
    env["NOTIFICATION_TEST_DATABASE_URL"] = database
    env["NOTIFICATION_TEST_REDIS_URL"] = redis_url
    result = subprocess.run(
        [sys.executable, "-m", "pytest", "tests", "-q", "--junitxml", str(output / "junit.xml")],
        cwd=ROOT, env=env, text=True, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        check=False,
    )
    (output / "pytest.log").write_text(result.stdout, encoding="utf-8")

    cases = {}
    junit = output / "junit.xml"
    if junit.exists():
        for case in ET.parse(junit).iter("testcase"):
            name = case.get("name", "")
            cases[name] = ("failed" if case.find("failure") is not None or case.find("error") is not None
                           else "skipped" if case.find("skipped") is not None else "passed")
    checks = {name: {"status": "passed" if all(cases.get(test) == "passed" for test in tests) else "failed",
                     "tests": {test: cases.get(test, "missing") for test in tests}}
              for name, tests in CHECKS.items()}
    report = {"run_id": run_id, "finished_at": datetime.now().astimezone().isoformat(),
              "pytest_exit_code": result.returncode, "test_cases": cases, "checklist": checks,
              "scope": "本机隔离 MySQL、独立 Redis 命名空间、ASGI 路由、飞书 HTTP 模拟传输、模拟审批适配器、隔离 Hermes 配置",
              "limitations": [
                  "ASGI 路由、Worker 投递函数和 Hermes 配置在进程内验证；未运行常驻服务或 Hermes ticker",
                  "未连接真实飞书卡片渲染或财务/OA 审批写入环境",
              ]}
    (output / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# 07i02 主动通知自动验收", "", f"运行编号：{run_id}",
             f"pytest 退出码：{result.returncode}", "", "| 验收项 | 状态 | 测试 |", "|---|---|---|"]
    for name, item in checks.items():
        lines.append(f"| {name} | {item['status']} | {', '.join(item['tests'])} |")
    lines += ["", "详细结果见 report.json、junit.xml 和 pytest.log。",
              "ASGI 路由、Worker 投递函数和 Hermes 配置在进程内验证；常驻服务与 Hermes ticker 尚未联调。",
              "真实飞书卡片展示及真实财务/OA 审批写入需在外部测试环境另行联调。", ""]
    (output / "report.md").write_text("\n".join(lines), encoding="utf-8")
    print(f"验收报告：{output / 'report.md'}")
    print(result.stdout[-3000:])
    return result.returncode or (1 if any(item["status"] != "passed" for item in checks.values()) else 0)


if __name__ == "__main__":
    raise SystemExit(main())
