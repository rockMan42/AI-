from datetime import date
from decimal import Decimal

from sqlalchemy import (
    BigInteger, Boolean, Date, Integer, JSON, Numeric, String,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class BusinessTrip(Base):
    __tablename__ = "t_business_trip"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    user_id: Mapped[int] = mapped_column(BigInteger)
    destination: Mapped[str] = mapped_column(String(200))
    start_date: Mapped[date] = mapped_column(Date)
    end_date: Mapped[date] = mapped_column(Date)
    purpose: Mapped[str | None] = mapped_column(String(500))
    budget: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    status: Mapped[str] = mapped_column(String(20))
    approver_id: Mapped[int | None] = mapped_column(BigInteger)


class ExpenseRule(Base):
    __tablename__ = "t_expense_rule"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    rule_name: Mapped[str] = mapped_column(String(100), unique=True)
    rule_category: Mapped[str] = mapped_column(String(30))
    city_tier: Mapped[str] = mapped_column(String(20))
    position_level: Mapped[str] = mapped_column(String(20))
    expense_type: Mapped[str] = mapped_column(String(30))
    max_amount: Mapped[Decimal | None] = mapped_column(Numeric(12, 2))
    effective_date: Mapped[date] = mapped_column(Date)
    expiry_date: Mapped[date | None] = mapped_column(Date)
    status: Mapped[str] = mapped_column(String(20))
    version: Mapped[str] = mapped_column(String(20))

    scope: Mapped[str] = mapped_column(String(20))
    conflict_group: Mapped[str] = mapped_column(String(50))
    priority: Mapped[int] = mapped_column(Integer)
    condition_json: Mapped[dict] = mapped_column(JSON)
    error_message: Mapped[str] = mapped_column(String(500))
    suggestion: Mapped[str] = mapped_column(String(500))
    severity: Mapped[str] = mapped_column(String(20))
    allow_override: Mapped[bool] = mapped_column(Boolean)


class ExpensePolicy(Base):
    __tablename__ = "t_expense_policy"

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True)
    revision: Mapped[int] = mapped_column(BigInteger, default=1)
    config_json: Mapped[dict] = mapped_column(JSON)


class ExpenseInvoiceClaim(Base):
    __tablename__ = "t_expense_invoice_claim"

    invoice_key: Mapped[str] = mapped_column(
        String(64), primary_key=True,
    )
    expense_id: Mapped[int] = mapped_column(BigInteger)


class ExpenseRuleAudit(Base):
    __tablename__ = "t_expense_rule_audit"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    operator_id: Mapped[int] = mapped_column(BigInteger)
    target: Mapped[str] = mapped_column(String(100))
    before_json: Mapped[dict | None] = mapped_column(JSON)
    after_json: Mapped[dict] = mapped_column(JSON)


class FinanceExpenseMock(Base):
    __tablename__ = "t_finance_expense_mock"

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    expense_id: Mapped[int] = mapped_column(BigInteger, unique=True)
    finance_no: Mapped[str] = mapped_column(String(40), unique=True)
    payload_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(30))
    approval_state: Mapped[dict | None] = mapped_column(JSON)