from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.leave_workflow import (
    ApprovalRecord,
    LeaveDraft,
    LeaveNotification,
    WorkCalendarDay,
)


ALTER_SQL = """
ALTER TABLE t_leave_request
    ADD COLUMN request_id VARCHAR(64) NULL,
    ADD COLUMN draft_id VARCHAR(32) NULL,
    ADD COLUMN year_days JSON NULL,
    ADD COLUMN approve_time DATETIME NULL,
    ADD COLUMN reject_reason VARCHAR(512) NULL,
    ADD COLUMN escalated_at DATETIME NULL,
    MODIFY COLUMN reason VARCHAR(1024) NULL,
    ADD UNIQUE KEY uk_leave_request_business_id (request_id),
    ADD UNIQUE KEY uk_leave_request_draft_id (draft_id);
""".strip()


def main():
    print("-- 请先核对实际表结构；本脚本只输出SQL，不执行迁移。")
    print("-- 适用于尚未添加这些字段的数据库，不可重复执行ALTER。")
    print(ALTER_SQL)

    dialect = mysql.dialect()
    for model in (
        WorkCalendarDay,
        LeaveDraft,
        ApprovalRecord,
        LeaveNotification,
    ):
        table = model.__table__
        print(str(CreateTable(table).compile(dialect=dialect)) + ";")
        for index in sorted(table.indexes, key=lambda item: item.name):
            print(str(CreateIndex(index).compile(dialect=dialect)) + ";")


if __name__ == "__main__":
    main()