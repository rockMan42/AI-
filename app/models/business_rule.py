from datetime import datetime

from sqlalchemy import (
    BigInteger,
    DateTime,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    Index,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base

TABLE_OPTIONS = {"mysql_engine": "InnoDB", "mysql_charset": "utf8mb4"}


class BusinessRule(Base):
    __tablename__ = "business_rule"
    __table_args__ = TABLE_OPTIONS

    rule_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    updated_by: Mapped[int] = mapped_column(Integer, nullable=False)


class RuleVersionHistory(Base):
    __tablename__ = "rule_version_history"
    __table_args__ = TABLE_OPTIONS

    rule_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    version: Mapped[int] = mapped_column(Integer, primary_key=True)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    content_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    changed_by: Mapped[int] = mapped_column(Integer, nullable=False)
    change_summary: Mapped[str] = mapped_column(String(512), nullable=False)
    rollback_from_version: Mapped[int | None] = mapped_column(Integer)


class RuleDocBinding(Base):
    __tablename__ = "rule_doc_binding"
    __table_args__ = TABLE_OPTIONS

    rule_type: Mapped[str] = mapped_column(String(32), primary_key=True)
    doc_id: Mapped[str] = mapped_column(String(128), primary_key=True)
    doc_title: Mapped[str] = mapped_column(String(256), default="")
    acknowledged_revision: Mapped[int] = mapped_column(BigInteger)
    observed_revision: Mapped[int] = mapped_column(BigInteger)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime)
    error: Mapped[str | None] = mapped_column(String(128))


class RuleChangeRequest(Base):
    __tablename__ = "rule_change_request"
    __table_args__ = (
        UniqueConstraint("actor_id", "request_key", name="uk_rule_change_key"),
        TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    actor_id: Mapped[int] = mapped_column(Integer)
    rule_type: Mapped[str] = mapped_column(String(32))
    request_key: Mapped[str] = mapped_column(String(128))
    request_hash: Mapped[str] = mapped_column(String(64))
    payload: Mapped[dict] = mapped_column(JSON)
    token_hash: Mapped[str | None] = mapped_column(String(64))
    expires_at: Mapped[datetime] = mapped_column(DateTime)
    result: Mapped[dict | None] = mapped_column(JSON)


class RuleOutbox(Base):
    __tablename__ = "rule_outbox"
    __table_args__ = (
        Index("idx_rule_outbox_due", "status", "next_attempt_at"),
        TABLE_OPTIONS,
    )

    id: Mapped[str] = mapped_column(String(128), primary_key=True)
    kind: Mapped[str] = mapped_column(String(32))
    payload: Mapped[dict] = mapped_column(JSON)
    status: Mapped[str] = mapped_column(String(16), default="pending")
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime)
    lease_token: Mapped[str | None] = mapped_column(String(32))
    last_error: Mapped[str | None] = mapped_column(String(128))
    remote_id: Mapped[str | None] = mapped_column(String(128))
