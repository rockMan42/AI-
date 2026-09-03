

from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import Integer, String, Index, UniqueConstraint, ForeignKeyConstraint

from app.models.base import Base


class User(Base):
    __tablename__ = "t_user"

    # 创建索引
    __table_args__ = (
        # 1. 普通索引（对应 SQL 的 INDEX）
        Index("idx_user_dept", "department_id"),
        Index("idx_user_role", "role"),

        # 2. 唯一约束（对应 SQL 的 UNIQUE KEY）
        UniqueConstraint("feishu_open_id", name="uk_feishu_open_id"),

        # 3. 外键约束（对应 SQL 的 CONSTRAINT fk_user_dept FOREIGN KEY）
        ForeignKeyConstraint(["department_id"], ["t_department.department_id"], name="fk_user_dept"),
    )

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True, comment='用户ID')
    feishu_open_id: Mapped[str] = mapped_column(String(64), nullable=False, comment="飞书OpenId")
    name: Mapped[str] = mapped_column(String(50), nullable=False, comment="姓名")
    department_id: Mapped[int] = mapped_column(Integer, nullable=False, comment="所属部门ID")
    role: Mapped[str] = mapped_column(String(20), nullable=False,default="员工",comment="角色：管理员/经理/员工")
    position_level: Mapped[str] = mapped_column(String(20), comment="职级，如P5、P6、M1")
    phone: Mapped[str] = mapped_column(String(20), comment="手机号")
    email: Mapped[str] = mapped_column(String(100), comment="邮箱")
    status: Mapped[str] = mapped_column(String(20), default="活动", comment="状态：活动/不活动")
