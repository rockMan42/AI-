from datetime import date, datetime
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    Date,
    DateTime,
    ForeignKey,
    Index,
    JSON,
    Numeric,
    String,
    Integer,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class Expense(Base):
    __tablename__ = "t_expense"

    __table_args__ = (
        Index("idx_exp_user", "user_id"),
        Index("idx_exp_trip", "trip_id"),
        Index("idx_exp_status", "status"),
        Index("idx_exp_approver", "approver_id"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("t_user.user_id"),
    )

    # 出差模型尚未实现；数据库已有的外键保持不变。
    trip_id: Mapped[int | None] = mapped_column(BigInteger)

    total_amount: Mapped[Decimal] = mapped_column(
        Numeric(12, 2), server_default="0.00",
    )
    status: Mapped[str] = mapped_column(
        String(20), server_default="draft",
    )
    submitter_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("t_user.user_id"),
    )
    approver_id: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("t_user.user_id"),
    )
    expense_no: Mapped[str | None] = mapped_column(
        String(40), unique=True,
    )
    source_key: Mapped[str | None] = mapped_column(
        String(64), unique=True,
    )
    payload: Mapped[dict | None] = mapped_column(JSON)

    has_override: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False,
        server_default="0",
    )
    override_reason: Mapped[str | None] = mapped_column(
        String(500),
    )
    finance_no: Mapped[str | None] = mapped_column(String(40))
    submitted_at: Mapped[datetime | None] = mapped_column(DateTime)

    submit_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0",
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime)
    last_error: Mapped[str | None] = mapped_column(String(500))
    notified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="0",
    )


class ExpenseItem(Base):
    __tablename__ = "t_expense_item"
    updated_at = None

    __table_args__ = (
        Index("idx_expi_expense", "expense_id"),
        Index("idx_expi_category", "category"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True,
    )
    expense_id: Mapped[int] = mapped_column(
        BigInteger, ForeignKey("t_expense.id"),
    )
    category: Mapped[str] = mapped_column(String(50))
    amount: Mapped[Decimal] = mapped_column(Numeric(12, 2))
    invoice_no: Mapped[str | None] = mapped_column(String(50))
    invoice_date: Mapped[date | None] = mapped_column(Date)
    description: Mapped[str | None] = mapped_column(String(500))


class Invoice(Base):
    __tablename__ = "t_invoice"
    updated_at = None

    __table_args__ = (
        Index("idx_inv_item", "expense_item_id"),
        Index("idx_inv_verified", "verified"),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True,
    )
    expense_item_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("t_expense_item.id"),
        nullable=True,
    )
    image_url: Mapped[str] = mapped_column(String(500))
    ocr_result_json: Mapped[dict | None] = mapped_column(JSON)

    verified: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    verified_by: Mapped[int | None] = mapped_column(
        BigInteger, ForeignKey("t_user.user_id"),
    )
    verified_at: Mapped[datetime | None] = mapped_column(DateTime)
