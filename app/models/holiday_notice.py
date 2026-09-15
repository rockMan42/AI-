from datetime import date

from sqlalchemy.orm import mapped_column, Mapped

from app.models.base import Base

from sqlalchemy import Index, String, BigInteger, Date, Text, ForeignKey, func


class HolidayNotice(Base):
    __tablename__ = "t_holiday_notice"

    __table_args__ = (
        Index("idx_holiday_status","status"),
        Index("idx_holiday_date","start_date","end_date")
    )

    id: Mapped[int] = mapped_column(BigInteger, primary_key=True, autoincrement=True)

    title: Mapped[str] = mapped_column(String(200), nullable=False)

    holiday_name: Mapped[str] = mapped_column(String(50), nullable=False)

    start_date: Mapped[date] = mapped_column(Date, nullable=False)

    end_date: Mapped[date] = mapped_column(Date, nullable=False)

    workday_arrangement: Mapped[str | None] = mapped_column(Text, nullable=True)

    notice_content: Mapped[str] = mapped_column(Text, nullable=False, default="")

    status:Mapped[str] = mapped_column(String(20), nullable=False, default="draft", server_default="draft")

    created_by: Mapped[int] = mapped_column(BigInteger, ForeignKey("t_user.user_id"),nullable=False)

    updated_at: Mapped[date] = mapped_column(Date, server_default="CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP", onupdate=func.current_timestamp())


