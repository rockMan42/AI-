from datetime import datetime

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


TABLE_OPTIONS = {
    "mysql_engine": "InnoDB",
    "mysql_charset": "utf8mb4",
    "mysql_collate": "utf8mb4_unicode_ci",
}


class Requisition(Base):
    """
    对应现有 t_requisition。

    注意：
    - purpose、expected_return_date 不在现有表中；
    - 两项仅在 Redis 草稿和 OA 请求中存在；
    - 本地 id 与 OA requisition_id 必须一致。
    """

    __tablename__ = "t_requisition"

    __table_args__ = (
        Index("idx_req_user", "user_id"),
        Index("idx_req_status", "status"),
        Index("idx_req_approver", "approver_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="申领单ID",
    )
    user_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("t_user.user_id"),
        nullable=False,
        comment="申领人",
    )
    item_category: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        comment="物资分类",
    )
    item_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
        comment="物资名称",
    )
    specification: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
        comment="规格型号",
    )
    quantity: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=1,
        server_default="1",
        comment="数量",
    )
    reason: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="申领原因",
    )
    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="pending",
        server_default="pending",
        comment="pending/approved/rejected/fulfilled",
    )
    approver_id: Mapped[int | None] = mapped_column(
        BigInteger,
        ForeignKey("t_user.user_id"),
        nullable=True,
        comment="当前审批人",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        comment="创建时间",
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=text("CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP"),
        onupdate=func.current_timestamp(),
        comment="更新时间",
    )
    node_started_at: Mapped[datetime | None] = mapped_column(DateTime)


class RequisitionApproval(Base):
    """对应现有 t_requisition_approval。"""

    __tablename__ = "t_requisition_approval"

    # 现有表没有 updated_at，覆盖 Base 自动声明的字段。
    updated_at = None

    __table_args__ = (
        Index("idx_reqap_req", "requisition_id"),
        Index("idx_reqap_approver", "approver_id"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
        comment="审批记录ID",
    )
    requisition_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("t_requisition.id"),
        nullable=False,
        comment="申领单ID",
    )
    approver_id: Mapped[int] = mapped_column(
        BigInteger,
        ForeignKey("t_user.user_id"),
        nullable=False,
        comment="审批人",
    )
    action: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        comment="approve/reject",
    )
    comment: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="审批意见",
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime,
        nullable=False,
        server_default=func.current_timestamp(),
        comment="审批时间",
    )


class CategoryFieldRule(Base):
    __tablename__ = "t_category_field_rule"

    __table_args__ = (
        UniqueConstraint(
            "category",
            "field_name",
            name="uk_category_field",
        ),
        Index("idx_category_field_rule_category", "category"),
        TABLE_OPTIONS,
    )

    id: Mapped[int] = mapped_column(
        BigInteger,
        primary_key=True,
        autoincrement=True,
    )
    category: Mapped[str] = mapped_column(String(50), nullable=False)
    field_name: Mapped[str] = mapped_column(String(50), nullable=False)
    display_name: Mapped[str] = mapped_column(String(50), nullable=False)
    required: Mapped[bool] = mapped_column(
        Boolean,
        nullable=False,
        default=False,
        server_default="0",
    )
    default_value: Mapped[str | None] = mapped_column(
        String(100),
        nullable=True,
    )
    validation_rule: Mapped[str | None] = mapped_column(
        String(200),
        nullable=True,
    )
    sort_order: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        default=0,
        server_default="0",
    )
