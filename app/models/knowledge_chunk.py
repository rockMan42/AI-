from __future__ import annotations

from typing import TYPE_CHECKING

from sqlalchemy import (
    BigInteger,
    Boolean,
    CheckConstraint,
    ForeignKeyConstraint,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.models.base import Base


if TYPE_CHECKING:
    from app.models.knowledge_document import KnowledgeDocument


class KnowledgeChunk(Base):
    __tablename__ = "kb_chunk"
    __table_args__ = (
        UniqueConstraint(
            "doc_id",
            "doc_version",
            "chunk_index",
            name="uk_kb_chunk_doc_version_index",
        ),
        ForeignKeyConstraint(
            ["doc_id", "doc_version"],
            ["kb_document.doc_id", "kb_document.version"],
            name="fk_kb_chunk_document_version",
            ondelete="CASCADE",
        ),
        Index(
            "idx_kb_chunk_doc_active",
            "doc_id",
            "is_active",
        ),
        Index(
            "idx_kb_chunk_embedding_status",
            "embedding_status",
        ),
        Index(
            "idx_kb_chunk_permission",
            "permission_level",
        ),
        CheckConstraint(
            "embedding_status IN "
            "('pending', 'processing', 'embedded', 'failed')",
            name="ck_kb_chunk_embedding_status",
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
    doc_version: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    chunk_index: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    title_path: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    chunk_text: Mapped[str] = mapped_column(
        Text,
        nullable=False,
    )
    token_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    permission_level: Mapped[str] = mapped_column(
        String(32),
        nullable=False,
    )
    source_file: Mapped[str] = mapped_column(
        String(512),
        nullable=False,
    )
    milvus_id: Mapped[str | None] = mapped_column(
        String(128),
        nullable=True,
    )
    embedding_status: Mapped[str] = mapped_column(
        String(16),
        nullable=False,
        default="pending",
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
    )

    document: Mapped["KnowledgeDocument"] = relationship(
        "KnowledgeDocument",
        back_populates="chunks",
    )