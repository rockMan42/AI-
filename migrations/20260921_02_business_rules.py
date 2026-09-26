"""仅输出 DDL，不连接数据库。"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable
from app.models.business_rule import (
    BusinessRule,
    RuleVersionHistory,
    RuleDocBinding,
    RuleChangeRequest,
    RuleOutbox,
)


def main():
    dialect = mysql.dialect()
    for model in (
        BusinessRule,
        RuleVersionHistory,
        RuleDocBinding,
        RuleChangeRequest,
        RuleOutbox,
    ):
        table = model.__table__
        print(str(CreateTable(table).compile(dialect=dialect)) + ";")
        for index in sorted(table.indexes, key=lambda i: i.name):
            print(str(CreateIndex(index).compile(dialect=dialect)) + ";")


if __name__ == "__main__":
    main()
