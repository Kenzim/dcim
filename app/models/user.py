from typing import Optional

from sqlalchemy import Column, Integer, String, Boolean, ForeignKey, UniqueConstraint, DateTime
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func
import bcrypt
from app.core.database import Base


_ON_DELETE_SET_NULL = "SET NULL"

# Minimum password length enforced for every account (including admins).
# There was previously no floor at all; 8 is the NIST 800-63B baseline
# minimum and doesn't require rewriting the many existing test fixtures
# that already use realistic 9+ character passwords.
MIN_PASSWORD_LENGTH = 8

# Fixed bcrypt hash used purely to burn a comparable amount of CPU time when
# no matching user exists at login, so "unknown username" and "wrong
# password" responses take roughly the same amount of time and can't be
# distinguished via timing side channel. This is not a real credential.
_DUMMY_PASSWORD_HASH = bcrypt.hashpw(bcrypt.gensalt(), bcrypt.gensalt()).decode('utf-8')


def verify_password_timing_safe_dummy(password: Optional[str]) -> None:
    """Run a throwaway bcrypt comparison to mimic the cost of ``verify_password``.

    Call this on the "user not found" branch of login so that responses for
    unknown usernames take about as long as responses for known usernames
    with a wrong password, closing the username-enumeration timing gap.
    """
    try:
        bcrypt.checkpw((password or "").encode('utf-8')[:72], _DUMMY_PASSWORD_HASH.encode('utf-8'))
    except Exception:
        pass


class User(Base):
    __tablename__ = "users"
    __table_args__ = (
        # NULLs don't collide under MySQL uniqueness, so plenty of users with
        # no billing identity (admins, native-only clients) can all have
        # NULL/NULL here; only an actual (integration, external id) pair must
        # be unique.
        UniqueConstraint(
            "billing_integration_id", "external_user_id", name="uq_users_billing_identity"
        ),
        UniqueConstraint(
            "reseller_id", "external_user_id", name="uq_users_reseller_identity"
        ),
    )

    id = Column(Integer, primary_key=True, index=True)
    username = Column(String(255), unique=True, index=True, nullable=False)
    email = Column(String(255), unique=True, index=True, nullable=False)
    # Nullable: a blank/unset password means password login is disabled for
    # this account. Non-admin (client) accounts always have full client
    # portal access regardless (via admin impersonation and billing SSO);
    # a blank password only disables the direct username/password login path
    # until an admin sets a real one. Admin accounts always have a password.
    password = Column(String(255), nullable=True)
    is_admin = Column(Boolean, default=False, nullable=False)
    is_reseller = Column(Boolean, default=False, server_default="0", nullable=False)
    # A reseller account itself is linked through Reseller.user_id. This FK
    # instead identifies ordinary client users managed by that reseller.
    reseller_id = Column(
        Integer,
        ForeignKey(
            "resellers.id",
            name="fk_users_reseller_id",
            ondelete=_ON_DELETE_SET_NULL,
            use_alter=True,
        ),
        nullable=True,
        index=True,
    )
    # Client permission preset (non-admin users only; ignored for staff).
    # Sits above a product default and below per-service overrides in the
    # resolution hierarchy (see app.services.client_permission_resolver).
    permission_set_id = Column(
        Integer,
        ForeignKey("permission_sets.id", ondelete=_ON_DELETE_SET_NULL),
        nullable=True,
        index=True,
    )

    # Billing identity (formerly a separate ExternalUser row + identity-link
    # table). A user has at most one linked billing identity: which
    # integration issued it, its id/username/email in that external system.
    # All nullable — most admin/native-only accounts have none of these set.
    billing_integration_id = Column(
        Integer,
        ForeignKey(
            "billing_integrations.id", ondelete=_ON_DELETE_SET_NULL
        ),
        nullable=True,
        index=True,
    )
    external_user_id = Column(String(255), nullable=True, index=True)  # user id in the external system (e.g. WHMCS)
    external_username = Column(String(255), nullable=True)
    external_email = Column(String(255), nullable=True)

    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False)

    permission_set = relationship("PermissionSet", foreign_keys=[permission_set_id])
    billing_integration = relationship(
        "BillingIntegration", back_populates="billing_users", foreign_keys=[billing_integration_id]
    )
    reseller_account = relationship(
        "Reseller",
        back_populates="user",
        foreign_keys="Reseller.user_id",
        uselist=False,
    )
    owning_reseller = relationship(
        "Reseller",
        back_populates="clients",
        foreign_keys=[reseller_id],
    )

    @property
    def has_password(self) -> bool:
        """Whether direct username/password login is enabled for this account."""
        return bool(self.password)

    def set_password(self, password: Optional[str]) -> None:
        """Hash and set the password.

        Passing ``None`` or an empty string clears the password, which
        disables password-based login (blank password) without affecting
        any other sign-in path (impersonation, billing SSO).
        """
        if not password:
            self.password = None
            return
        if len(password) < MIN_PASSWORD_LENGTH:
            raise ValueError(f"Password must be at least {MIN_PASSWORD_LENGTH} characters long")
        # Bcrypt has a 72-byte limit, reject passwords that are too long
        password_bytes = password.encode('utf-8')
        if len(password_bytes) > 72:
            raise ValueError("Password cannot be longer than 72 bytes")
        self.password = bcrypt.hashpw(password_bytes, bcrypt.gensalt()).decode('utf-8')

    def verify_password(self, password: str) -> bool:
        """Verify a password against the stored hash. Always false when no password is set."""
        if not self.password or not password:
            return False
        # Bcrypt has a 72-byte limit, reject passwords that are too long
        password_bytes = password.encode('utf-8')
        if len(password_bytes) > 72:
            return False
        try:
            return bcrypt.checkpw(password_bytes, self.password.encode('utf-8'))
        except Exception:
            return False

