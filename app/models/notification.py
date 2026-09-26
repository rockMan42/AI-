from datetime import date, datetime

from sqlalchemy import BigInteger, Boolean, Date, DateTime, ForeignKey, Index, Integer, JSON, String, Text, UniqueConstraint
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class NotificationTemplate(Base):
    __tablename__ = "notification_template"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scene: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    title_template: Mapped[str] = mapped_column(String(256), nullable=False)
    body_template: Mapped[str] = mapped_column(Text, nullable=False)
    actions: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class NotificationSchedule(Base):
    __tablename__ = "notification_schedule"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scene: Mapped[str] = mapped_column(String(32), nullable=False, unique=True)
    cron_expr: Mapped[str] = mapped_column(String(64), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    retry_max: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retry_backoff_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=60)


class NotificationLog(Base):
    __tablename__ = "notification_log"
    __table_args__ = (
        UniqueConstraint("target_user_id", "send_date", "batch_key", name="uk_notification_batch"),
        Index("idx_notification_due", "status", "next_attempt_at"),
        Index("idx_notification_user_day", "target_user_id", "send_date"),
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    scene: Mapped[str] = mapped_column(String(32), nullable=False)
    target_user_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("t_user.user_id"), nullable=False)
    send_date: Mapped[date] = mapped_column(Date, nullable=False)
    batch_key: Mapped[str] = mapped_column(String(80), nullable=False)
    title: Mapped[str | None] = mapped_column(String(256))
    body: Mapped[str | None] = mapped_column(Text)
    card: Mapped[dict | None] = mapped_column(JSON)
    delivery_item_ids: Mapped[list] = mapped_column(JSON, nullable=False, default=list)
    status: Mapped[str] = mapped_column(String(20), nullable=False, default="PENDING")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    retry_max: Mapped[int] = mapped_column(Integer, nullable=False, default=3)
    retry_backoff_sec: Mapped[int] = mapped_column(Integer, nullable=False, default=60)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)
    first_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    feishu_message_id: Mapped[str | None] = mapped_column(String(100))
    error_msg: Mapped[str | None] = mapped_column(String(100))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime)
    read_at: Mapped[datetime | None] = mapped_column(DateTime)


class NotificationItem(Base):
    __tablename__ = "notification_item"
    __table_args__ = (Index("idx_notification_item_log", "log_id"),)

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)
    event_key: Mapped[str] = mapped_column(String(180), nullable=False, unique=True)
    log_id: Mapped[int] = mapped_column(BigInteger, ForeignKey("notification_log.id"), nullable=False)
    scene: Mapped[str] = mapped_column(String(32), nullable=False)
    source_id: Mapped[str] = mapped_column(String(64), nullable=False)
    source_version: Mapped[str] = mapped_column(String(64), nullable=False)
    title: Mapped[str] = mapped_column(String(256), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    action: Mapped[dict | None] = mapped_column(JSON)
    raw_card: Mapped[dict | None] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="PENDING")
    acted_at: Mapped[datetime | None] = mapped_column(DateTime)
    acted_by: Mapped[int | None] = mapped_column(Integer)
    acted_action: Mapped[str | None] = mapped_column(String(16))
