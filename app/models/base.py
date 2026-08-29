from sqlalchemy.orm import DeclarativeBase

from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column
from sqlalchemy import DATETIME, Integer, String, Index, UniqueConstraint, ForeignKeyConstraint, func

from datetime import datetime

class Base(DeclarativeBase):
    created_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now(), insert_default=datetime.now(),
                                                 comment="创建时间")
    updated_at: Mapped[datetime] = mapped_column(DATETIME, default=datetime.now(), insert_default=datetime.now(),
                                                 onupdate=func.now(), comment="更新时间")
