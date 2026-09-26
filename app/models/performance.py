from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class PerformanceReview(Base):
    __tablename__ = "t_performance"
    __table_args__ = (
        UniqueConstraint(
            "user_id", "period", name="uk_perf_user_period",
        ),
        Index("idx_perf_user", "user_id"),
        Index("idx_perf_period", "period"),
        Index("idx_perf_reviewer", "reviewer_id"),
        Index("idx_perf_status_deadline", "status", "deadline"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("t_user.user_id"), nullable=False,
    )
    reviewer_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("t_user.user_id"),
    )
    period: Mapped[str] = mapped_column(String(20), nullable=False)
    score: Mapped[int] = mapped_column(Integer, nullable=False)
    grade: Mapped[str] = mapped_column(String(5), nullable=False)
    rank_in_dept: Mapped[int | None] = mapped_column(Integer)
    reviewer_comment: Mapped[str | None] = mapped_column(Text)
    self_comment: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(
        String(20), nullable=False, default="draft",
        server_default="draft",
    )
    deadline: Mapped[date | None] = mapped_column(Date)
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)
    completed_at: Mapped[datetime | None] = mapped_column(DateTime)


class PerformanceStats(Base):
    __tablename__ = "performance_stats"
    __table_args__ = (
        UniqueConstraint(
            "cycle", "dimension", "dimension_value",
            name="uk_performance_stats_cycle_dim",
        ),
        Index("idx_performance_stats_cycle", "cycle"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    cycle: Mapped[str] = mapped_column(String(20), nullable=False)
    department_id: Mapped[int | None] = mapped_column(Integer)
    dimension: Mapped[str] = mapped_column(String(32), nullable=False)
    dimension_value: Mapped[str] = mapped_column(
        String(100), nullable=False,
    )
    avg_score: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    std_dev: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    min_score: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    max_score: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    p25: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    p50: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    p75: Mapped[Decimal] = mapped_column(Numeric(7, 2))
    total_count: Mapped[int] = mapped_column(Integer, nullable=False)
    computed_at: Mapped[datetime] = mapped_column(DateTime, nullable=False)


class PerformanceReminderLog(Base):
    __tablename__ = "reminder_log"
    __table_args__ = (
        Index("idx_performance_reminder_review", "review_id"),
        Index(
            "idx_performance_reminder_type",
            "review_id", "reminder_type",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    review_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("t_performance.id"),
        nullable=False,
    )
    reminder_type: Mapped[str] = mapped_column(
        String(32), nullable=False,
    )
    target_user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("t_user.user_id"),
        nullable=False,
    )
    channel: Mapped[str] = mapped_column(
        String(16), nullable=False, default="FEISHU",
        server_default="FEISHU",
    )
    status: Mapped[str] = mapped_column(
        String(16), nullable=False, default="SENT",
        server_default="SENT",
    )
