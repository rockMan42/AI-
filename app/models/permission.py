from sqlalchemy import (
    BigInteger,
    Boolean,
    Integer,
    JSON,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.models.base import Base


class OrganizationState(Base):
    __tablename__ = "organization_state"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    version: Mapped[int] = mapped_column(Integer, default=1)
    dirty: Mapped[bool] = mapped_column(Boolean, default=True)
    synced_at: Mapped[int] = mapped_column(BigInteger, default=0)
    snapshot: Mapped[dict] = mapped_column(JSON, default=dict)


class UserRole(Base):
    __tablename__ = "user_role"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    feishu_user_id: Mapped[str | None] = mapped_column(String(64))
    feishu_open_id: Mapped[str] = mapped_column(String(64), unique=True)
    role: Mapped[str] = mapped_column(String(16), index=True)
    role_source: Mapped[str] = mapped_column(String(16))
    version: Mapped[int] = mapped_column(Integer)
    profile: Mapped[dict] = mapped_column(JSON)


class RoleMappingRule(Base):
    __tablename__ = "role_mapping_rule"
    __table_args__ = (
        UniqueConstraint("kind", "subject", name="uk_role_rule_subject"),
    )

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    kind: Mapped[str] = mapped_column(String(16))
    subject: Mapped[str] = mapped_column(String(64))
    role: Mapped[str] = mapped_column(String(16))
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)


class AuditLog(Base):
    __tablename__ = "audit_log"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    action: Mapped[str] = mapped_column(String(128))
    resource_type: Mapped[str] = mapped_column(String(64))
    resource_id: Mapped[str | None] = mapped_column(String(64))
    result: Mapped[str] = mapped_column(String(16), index=True)
    detail: Mapped[dict] = mapped_column(JSON, default=dict)
    ip_address: Mapped[str | None] = mapped_column(String(45))


class DenialCounter(Base):
    __tablename__ = "permission_denial_counter"

    user_id: Mapped[int] = mapped_column(Integer, primary_key=True)
    count: Mapped[int] = mapped_column(Integer, default=0)


class PermissionAlert(Base):
    __tablename__ = "permission_alert"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer)
    recipient: Mapped[str] = mapped_column(String(64))
    delivered: Mapped[bool] = mapped_column(Boolean, default=False)


class PermissionEvent(Base):
    __tablename__ = "permission_event"

    id: Mapped[str] = mapped_column(String(128), primary_key=True)


class AuthSession(Base):
    __tablename__ = "auth_session"

    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    user_id: Mapped[int] = mapped_column(Integer, index=True)
    refresh_hash: Mapped[str] = mapped_column(String(64))
    expires_at: Mapped[int] = mapped_column(BigInteger)
    revoked: Mapped[bool] = mapped_column(Boolean, default=False)