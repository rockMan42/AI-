from datetime import datetime

from sqlalchemy import (
    BigInteger, Boolean, DateTime, ForeignKey,
    Index, Integer, JSON, String, UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class ExpenseApprovalLog(Base):
    __tablename__ = "t_expense_approval_log"
    updated_at = None

    __table_args__ = (
        UniqueConstraint(
            "expense_id", "version",
            name="uk_exp_approval_version",
        ),
        Index("idx_exp_approval_expense", "expense_id", "id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    expense_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("t_expense.id"), nullable=False,
    )
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    from_status: Mapped[str] = mapped_column(String(30))
    to_status: Mapped[str] = mapped_column(String(30))

    approver_id: Mapped[int | None] = mapped_column(
        Integer, ForeignKey("t_user.user_id"),
    )
    approver_name: Mapped[str | None] = mapped_column(String(50))
    comment: Mapped[str | None] = mapped_column(String(500))

    # 财务实际发生时间与本地入库时间分开保存。
    occurred_at: Mapped[datetime] = mapped_column(DateTime)
    detail: Mapped[dict] = mapped_column(JSON)

    notified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    notified_at: Mapped[datetime | None] = mapped_column(DateTime)


class ExpenseNotification(Base):
    __tablename__ = "t_expense_notification"

    __table_args__ = (
        Index("idx_exp_notice_due", "status", "next_attempt_at"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    event_key: Mapped[str] = mapped_column(String(180), unique=True)
    approval_log_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("t_expense_approval_log.id"),
    )
    receiver_id: Mapped[int] = mapped_column(
        Integer, ForeignKey("t_user.user_id"),
    )
    kind: Mapped[str] = mapped_column(String(20))
    payload: Mapped[dict] = mapped_column(JSON)

    status: Mapped[str] = mapped_column(
        String(20), default="pending", server_default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, default=0, server_default="0",
    )
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    message_id: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(String(100))
    