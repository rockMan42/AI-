"""输出 07i02 DDL 和默认数据；不连接或修改数据库。"""
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import User  # noqa: F401
from app.models.notification import NotificationItem, NotificationLog, NotificationSchedule, NotificationTemplate


DEFAULTS = {
    "approval_reminder": ("0 10 * * *", "{{ kind }}审批待处理", "{{ kind }}申请 {{ identifier }} 已在当前节点等待超过 48 小时，请尽快处理。"),
    "attendance_alert": ("30 9 * * *", "出勤异常提醒", "{{ employee_name }}于 {{ date }} 出现{{ issue }}，请核实。"),
    "lead_follow": ("0 10 * * 1-5", "客户线索待跟进", "{{ company_name }}已 {{ days_idle }} 天未跟进，请及时处理。"),
    "review_deadline": ("0 9 * * *", "考核评分截止提醒", "{{ period }} 考核距 {{ deadline }} 截止还有 {{ days_left }} 天，请完成评分。"),
}


def main():
    print("ALTER TABLE t_requisition ADD COLUMN node_started_at DATETIME NULL;")
    dialect = mysql.dialect()
    for model in (NotificationTemplate, NotificationSchedule, NotificationLog, NotificationItem):
        table = model.__table__
        print(f"{CreateTable(table).compile(dialect=dialect)};")
        for index in sorted(table.indexes, key=lambda item: item.name):
            print(f"{CreateIndex(index).compile(dialect=dialect)};")
    for scene, (cron, title, body) in DEFAULTS.items():
        args = ", ".join("'" + value.replace("'", "''") + "'" for value in (scene, title, body))
        print(f"INSERT INTO notification_template (scene,title_template,body_template,actions,is_active) VALUES ({args},'[]',1);")
        args = ", ".join("'" + value.replace("'", "''") + "'" for value in (scene, cron))
        print(f"INSERT INTO notification_schedule (scene,cron_expr,enabled,retry_max,retry_backoff_sec) VALUES ({args},1,3,60);")


if __name__ == "__main__":
    main()
