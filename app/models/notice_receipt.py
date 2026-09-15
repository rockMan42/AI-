

from datetime import  datetime

from sqlalchemy.orm import mapped_column, Mapped

from app.models.base import Base

from sqlalchemy import Index, BigInteger, ForeignKey, UniqueConstraint, Boolean, DateTime


class NoticeReceipt(Base):

    __tablename__ = "t_notice_receipt"

    # 对应的数据库不存在这个字段，既然继承了Base，就注释掉这个字段
    updated_at = None

    __table_args__ = (
        Index("idx_receipt_notice", "notice_id"),
        Index("idx_receipt_user", "user_id"),
        UniqueConstraint(
            "notice_id",
            "user_id",
            name="uk_receipt_notice_user",
        ),
    )

    id: Mapped[int] = mapped_column(
        BigInteger, primary_key=True, autoincrement=True,
    )
    notice_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("t_holiday_notice.id"),
        nullable=False,
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("t_user.user_id"),
        nullable=False,
    )
    confirmed: Mapped[bool] = mapped_column(
        Boolean, default=False, server_default="0",
    )
    confirmed_at: Mapped[datetime | None] = mapped_column(
        DateTime, nullable=True,
    )