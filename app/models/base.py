from datetime import datetime

from sqlalchemy import DATETIME, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(
        DATETIME,
        nullable=False,
        server_default=func.current_timestamp(),
        comment="创建时间，UTC",
    )

    updated_at: Mapped[datetime] = mapped_column(
        DATETIME,
        nullable=False,
        server_default=func.current_timestamp(),
        onupdate=func.current_timestamp(),
        comment="更新时间，UTC",
    )