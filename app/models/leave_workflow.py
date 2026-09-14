from datetime import date, datetime

from sqlalchemy import (
    BigInteger, Boolean, Date, DateTime, ForeignKey,
    Index, Integer, JSON, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class WorkCalendarDay(Base):
    __tablename__ = "t_work_calendar_day"
    __table_args__ = {**TABLE_OPTIONS}

    day: Mapped[date] = mapped_column(Date, primary_key=True)
    is_workday: Mapped[bool] = mapped_column(Boolean, nullable=False)
    remark: Mapped[str | None] = mapped_column(String(100))


class LeaveDraft(Base):
    __tablename__ = "t_leave_draft"
    __table_args__ = (
        Index("idx_leave_draft_user_status", "user_id", "status"),
        {**TABLE_OPTIONS},
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, nullable=False)
    flow_key: Mapped[str | None] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="ready",
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class ApprovalRecord(Base):
    __tablename__ = "t_approval_record"
    __table_args__ = (
        Index("idx_approval_record_request", "request_id", "id"),
        {**TABLE_OPTIONS},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    request_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("t_leave_request.request_id"),
        nullable=False,
    )
    action: Mapped[str] = mapped_column(String(20), nullable=False)
    operator_id: Mapped[int | None] = mapped_column(
        Integer, nullable=True, comment="NULL表示系统定时任务",
    )
    remark: Mapped[str] = mapped_column(
        String(512), nullable=False, default="",
    )


class LeaveNotification(Base):
    __tablename__ = "t_leave_notification"
    __table_args__ = (
        Index("idx_leave_notification_due", "status", "next_attempt_at"),
        {**TABLE_OPTIONS},
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    event_key: Mapped[str] = mapped_column(
        String(180), nullable=False, unique=True,
    )
    request_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("t_leave_request.request_id"),
        nullable=False,
    )
    kind: Mapped[str] = mapped_column(String(20), nullable=False)
    receiver_open_id: Mapped[str] = mapped_column(String(64), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="pending",
    )
    attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0,
    )
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    message_id: Mapped[str | None] = mapped_column(String(100))