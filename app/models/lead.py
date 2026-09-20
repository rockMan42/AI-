from datetime import datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    DateTime,
    ForeignKey,
    Index,
    Numeric,
    String,
    Text,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Lead(Base):
    __tablename__ = "t_lead"

    __table_args__ = (
        Index("idx_lead_status", "status"),
        Index("idx_lead_assigned", "assigned_to"),
        Index("idx_lead_priority", "priority"),
        Index("idx_lead_followup", "next_follow_up"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    company_name: Mapped[str] = mapped_column(String(200))
    contact_name: Mapped[str] = mapped_column(String(50))
    contact_phone: Mapped[str | None] = mapped_column(String(20))
    contact_email: Mapped[str | None] = mapped_column(String(100))
    budget: Mapped[Decimal | None] = mapped_column(
        Numeric(12, 2), comment="预算，万元",
    )
    source: Mapped[str] = mapped_column(String(50))
    status: Mapped[str] = mapped_column(
        String(30), server_default="new",
    )
    priority: Mapped[str] = mapped_column(
        String(10), server_default="medium",
    )
    priority_score: Mapped[Decimal] = mapped_column(
        Numeric(5, 2), server_default="0",
    )
    assigned_to: Mapped[int | None] = mapped_column(
        ForeignKey("t_user.user_id"),
    )
    remark: Mapped[str | None] = mapped_column(Text)
    last_follow_up: Mapped[datetime | None] = mapped_column(DateTime)
    next_follow_up: Mapped[datetime | None] = mapped_column(DateTime)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        server_default=text(
            "CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"
        ),
        onupdate=text("CURRENT_TIMESTAMP"),
        comment="更新时间，UTC",
    )


class LeadFollowUp(Base):
    __tablename__ = "t_lead_follow_up"

    __table_args__ = (
        Index("idx_lfu_lead", "lead_id"),
        Index("idx_lfu_user", "user_id"),
        Index("idx_lfu_next", "next_follow_up"),
    )

    # 跟进记录只追加，不继承 Base 的更新时间字段。
    updated_at = None

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    lead_id: Mapped[int] = mapped_column(
        ForeignKey("t_lead.id"), nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        ForeignKey("t_user.user_id"), nullable=False,
    )
    follow_up_type: Mapped[str] = mapped_column(String(20))
    content: Mapped[str] = mapped_column(Text)
    outcome: Mapped[str | None] = mapped_column(String(30))
    next_action: Mapped[str | None] = mapped_column(String(200))
    next_follow_up: Mapped[datetime | None] = mapped_column(DateTime)