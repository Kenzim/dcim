"""Extended authentication: external identities, tokens, and TOTP."""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    DateTime,
    Enum as SQLEnum,
    ForeignKey,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base

_FK_USERS = "users.id"


def _enum_values(enum_cls: type[enum.Enum]) -> list[str]:
    return [member.value for member in enum_cls]


def _string_enum(enum_cls: type[enum.Enum], *, length: int = 32) -> SQLEnum:
    return SQLEnum(
        enum_cls,
        native_enum=False,
        values_callable=_enum_values,
        length=length,
    )


class ExternalIdentityProvider(str, enum.Enum):
    DISCORD = "discord"


class UserExternalIdentity(Base):
    __tablename__ = "user_external_identities"
    __table_args__ = (
        UniqueConstraint(
            "provider",
            "provider_user_id",
            name="uq_user_external_identities_provider_user",
        ),
        UniqueConstraint(
            "user_id",
            "provider",
            name="uq_user_external_identities_user_provider",
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider = Column(_string_enum(ExternalIdentityProvider), nullable=False)
    provider_user_id = Column(String(255), nullable=False, index=True)
    username = Column(String(255), nullable=True)
    access_token_encrypted = Column(Text, nullable=True)
    refresh_token_encrypted = Column(Text, nullable=True)
    linked_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id = Column(Integer, primary_key=True, index=True)
    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash = Column(String(128), nullable=False, unique=True, index=True)
    expires_at = Column(DateTime(timezone=True), nullable=False, index=True)
    consumed_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)

    user = relationship("User", foreign_keys=[user_id])


class UserTotpSecret(Base):
    __tablename__ = "user_totp_secrets"

    user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete="CASCADE"),
        primary_key=True,
    )
    secret_encrypted = Column(Text, nullable=False)
    enabled = Column(Boolean, nullable=False, default=False, server_default="0")
    recovery_codes_hash = Column(JSON, nullable=False, default=list)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    confirmed_at = Column(DateTime(timezone=True), nullable=True)

    user = relationship("User", foreign_keys=[user_id])
