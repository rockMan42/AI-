from sqlalchemy.dialects import mysql
from sqlalchemy.schema import CreateIndex, CreateTable

from app.models import Lead, LeadFollowUp


def main():
    print("-- 本脚本只输出SQL，不连接数据库，不执行迁移。")
    print("-- 执行前核对 t_user.user_id 的实际类型。")
    print("-- CREATE TABLE 仅适用于尚未创建对应表的数据库。")

    dialect = mysql.dialect()
    for model in (Lead, LeadFollowUp):
        table = model.__table__
        print(str(CreateTable(table).compile(dialect=dialect)) + ";")
        for index in sorted(table.indexes, key=lambda item: item.name):
            print(str(CreateIndex(index).compile(dialect=dialect)) + ";")


if __name__ == "__main__":
    main()