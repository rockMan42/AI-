from datetime import date, datetime
from decimal import Decimal
from sqlalchemy.orm import Mapped, mapped_column
from sqlalchemy import BigInteger, String, Date, DateTime, Numeric, Index, UniqueConstraint, ForeignKeyConstraint, func
from app.models.base import Base


class Attendance(Base):
    __tablename__ = "t_attendance"

    # 表级约束与索引配置
    __table_args__ = (
        # 1. 唯一约束（对应 SQL 的 UNIQUE KEY idx_att_user_date）
        UniqueConstraint("user_id", "date", name="idx_att_user_date"),

        # 2. 普通索引（对应 SQL 的 INDEX idx_att_status）
        Index("idx_att_status", "status"),

        # 3. 外键约束（对应 SQL 的 CONSTRAINT fk_att_user FOREIGN KEY）
        ForeignKeyConstraint(["user_id"], ["t_user.user_id"], name="fk_att_user"),
    )

    # 字段映射
    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True, comment='考勤记录ID')
    user_id: Mapped[int] = mapped_column(BigInteger, nullable=False, comment='用户ID')
    date: Mapped[date] = mapped_column(Date, nullable=False, comment='考勤日期')
    clock_in_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment='上班打卡时间')
    clock_out_time: Mapped[datetime | None] = mapped_column(DateTime, nullable=True, comment='下班打卡时间')
    status: Mapped[str] = mapped_column(String(20), nullable=False, default='正常', comment='状态：normal/late/early_leave/absent/leave')
    work_hours: Mapped[Decimal | None] = mapped_column(Numeric(4, 1), nullable=True, comment='工时（小时）')
    remark: Mapped[str | None] = mapped_column(String(200), nullable=True, comment='备注')
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.now(),  # 对应 SQL 的 DEFAULT CURRENT_TIMESTAMP
        comment='创建时间'
    )