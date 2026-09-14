from datetime import datetime
from decimal import Decimal
from sqlalchemy import JSON

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Numeric,
    String,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LeaveRequest(Base):
    __tablename__ = "t_leave_request"

    __table_args__ = (
        Index("idx_leave_user", "user_id"),
        Index("idx_leave_status", "status"),
        Index("idx_leave_period", "start_time", "end_time"),
        ForeignKeyConstraint(
            ["user_id"],
            ["t_user.user_id"],
            name="fk_leave_user",
        ),
        ForeignKeyConstraint(
            ["approver_id"],
            ["t_user.user_id"],
            name="fk_leave_approver",
        ),
        {
            "comment": "请假申请表",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="请假申请ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="申请人"
    )
    leave_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="假别：annual/sick/personal/marriage/maternity",
    )
    start_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="开始时间"
    )
    end_time: Mapped[datetime] = mapped_column(
        DateTime, nullable=False, comment="结束时间"
    )
    duration: Mapped[Decimal] = mapped_column(
        Numeric(4, 1), nullable=False, comment="时长（天）"
    )
    reason: Mapped[str | None] = mapped_column(
        String(1024), nullable=True, comment="请假原因",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="状态：pending/approved/rejected/cancelled",
    )
    approver_id: Mapped[int | None] = mapped_column(
        BigInteger, nullable=True, comment="审批人"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        onupdate=func.current_timestamp(),
        comment="更新时间",
    )
    # 历史数据允许为空；新流程创建的申请必须填写。
    request_id: Mapped[str | None] = mapped_column(
        String(64), nullable=True, unique=True,
        comment="申请业务编号",
    )
    draft_id: Mapped[str | None] = mapped_column(
        String(32), nullable=True, unique=True,
        comment="来源确认草稿，用于提交幂等",
    )
    year_days: Mapped[dict | None] = mapped_column(
        JSON, nullable=True,
        comment="各年度扣减天数快照",
    )
    approve_time: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )
    reject_reason: Mapped[str | None] = mapped_column(
        String(512), nullable=True,
    )
    escalated_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
        comment="首次升级时间；升级不改变pending状态",
    )
