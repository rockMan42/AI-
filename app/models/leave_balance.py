from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class LeaveBalance(Base):
    __tablename__ = "t_leave_balance"

    __table_args__ = (
        UniqueConstraint(
            "user_id",
            "leave_type",
            "year",
            name="uk_leave_balance_user_type_year",
        ),
        Index("idx_lb_user", "user_id"),
        ForeignKeyConstraint(
            ["user_id"],
            ["t_user.user_id"],
            name="fk_lb_user",
        ),
        {
            "comment": "假期余额表",
            "mysql_engine": "InnoDB",
            "mysql_charset": "utf8mb4",
            "mysql_collate": "utf8mb4_unicode_ci",
        },
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True, comment="余额记录ID"
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, nullable=False, comment="用户ID"
    )
    leave_type: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="假别：annual/sick/personal/marriage/maternity",
    )
    total_days: Mapped[Decimal] = mapped_column(
        Numeric(4, 1), nullable=False, comment="年度总天数"
    )
    used_days: Mapped[Decimal] = mapped_column(
        Numeric(4, 1),
        nullable=False,
        default=Decimal("0"),
        server_default="0",
        comment="已使用天数",
    )
    remaining_days: Mapped[Decimal] = mapped_column(
        Numeric(4, 1), nullable=False, comment="剩余天数"
    )
    year: Mapped[int] = mapped_column(Integer, nullable=False, comment="年度")
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
