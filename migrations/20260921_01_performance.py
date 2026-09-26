import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models.performance import (
    PerformanceReminderLog,
    PerformanceStats,
)
from app.models.user import User  # noqa: F401 - 注册外键目标表


def main():
    print("""
ALTER TABLE t_performance
    ADD COLUMN deadline DATE NULL COMMENT '考核截止日期',
    ADD COLUMN submitted_at DATETIME NULL COMMENT '提交时间',
    ADD COLUMN completed_at DATETIME NULL COMMENT '完成时间',
    ADD INDEX idx_perf_status_deadline (status, deadline);
""".strip())

    dialect = mysql.dialect()
    for model in (PerformanceStats, PerformanceReminderLog):
        table = model.__table__
        print(f"{CreateTable(table).compile(dialect=dialect)};")
        for index in sorted(table.indexes, key=lambda item: item.name):
            print(f"{CreateIndex(index).compile(dialect=dialect)};")


if __name__ == "__main__":
    main()
