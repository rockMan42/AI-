from sqlalchemy import BigInteger, Boolean, Float, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class KnowledgeSearchLog(Base):
    __tablename__ = "kb_search_log"

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    user_id: Mapped[str] = mapped_column(
        String(64),
        nullable=False,
        comment="用户ID的HMAC标识",
    )
    query: Mapped[str] = mapped_column(
        Text,
        nullable=False,
        comment="Fernet加密后的问题",
    )
    top_score: Mapped[float] = mapped_column(
        Float,
        nullable=False,
    )
    result_count: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
    )
    is_low_confidence: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
    )