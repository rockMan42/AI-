from __future__ import annotations

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


if TYPE_CHECKING:
    from app.models.knowledge_chunk import KnowledgeChunk


class KnowledgeDocument(Base):
    __tablename__ = "kb_document"
    __table_args__ = (
        UniqueConstraint(
            "doc_id",
            "version",
            name="uk_kb_document_doc_version",
        ),
        Index("idx_kb_document_doc_id", "doc_id"),
        Index("idx_kb_document_status", "status"),
        Index(
            "idx_kb_document_permission",
            "permission_level",
        ),
        CheckConstraint(
            "permission_level IN "
            "('public', 'internal', 'confidential')",
            name="ck_kb_document_permission",
        ),
        CheckConstraint(
            "status IN ("
            "'parsing', 'completed', 'embedding', "
            "'embedded', 'embedding_failed', 'failed', "
            "'deleting', 'delete_failed'"
            ")",
            name="ck_kb_document_status",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    doc_id: Mapped[str] = mapped_column(
        String(128),
        nullable=False,
    )
    version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="v1.0",
    )
    title: Mapped[str] = mapped_column(
        String(256),
        nullable=False,
    )
    source_file: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    object_key: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    file_type: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
    )
    file_size: Mapped[int] = mapped_column(
        BigInteger,
        nullable=False,
    )
    sha256: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
    )
    permission_level: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="internal",
    )
    chunk_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    status: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
        default="parsing",
    )
    embedding_attempts: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
    )
    embedding_started_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    embedded_at: Mapped[datetime | None] = mapped_column(
        DateTime,
        nullable=True,
    )
    error_message: Mapped[str | None] = mapped_column(
        String(512),
        nullable=True,
    )
    uploaded_by: Mapped[int] = mapped_column(
        Integer,
        ForeignKey(
            "t_user.user_id",
            ondelete="RESTRICT",
        ),
        nullable=False,
    )

    chunks: Mapped[list["KnowledgeChunk"]] = relationship(
        "KnowledgeChunk",
        back_populates="document",
        cascade="all, delete-orphan",
        passive_deletes=True,
    )