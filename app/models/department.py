from sqlalchemy import String, Integer, Index, ForeignKeyConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

"""
飞书部门ID "0" 或 "od-xxx"
        ↓ 用于查询和同步
t_department.feishu_department_id

本地自增主键 1、2、3
        ↓ 用作数据库外键
t_department.department_id
        ↓
t_user.department_id
"""

class Department(Base):
    __tablename__ = "t_department"

    __table_args__ = (
        Index("idx_dept_parent", "parent_id"),
        Index("idx_dept_manager", "manager_user_id"),
        ForeignKeyConstraint(
            ["parent_id"],
            ["t_department.department_id"],
            name="fk_dept_parent"
        ),
        # 注意：manager_user_id 虽然建表语句未定义外键，但若需要关联 User，可添加：
        # ForeignKeyConstraint(["manager_user_id"], ["t_user.user_id"], name="fk_dept_manager"),
    )

    department_id: Mapped[int] = mapped_column(
        Integer, primary_key=True, autoincrement=True, comment="本地部门ID"
    )
    feishu_department_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        unique=True,
        comment="飞书部门ID",
    )
    name: Mapped[str] = mapped_column(
        String(100), nullable=False, comment="部门名称"
    )
    parent_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="上级部门ID，严格为NULL"
    )
    manager_user_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="部门负责人"
    )
    sort_order: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, comment="排序权重"
    )