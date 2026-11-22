"""Tax tables of billing phase 2 (docs/billing-phase2-design.md section 1).

Rates are exact decimals (``Numeric(9, 4)`` percentages); the engine converts
them once to integer parts per million before any arithmetic (design 4.4).
Amounts are integer minor units.
"""

from __future__ import annotations

import enum

from sqlalchemy import (
    BigInteger,
    Boolean,
    Column,
    Date,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    Numeric,
    SmallInteger,
    String,
    UniqueConstraint,
)
from sqlalchemy.orm import relationship
from sqlalchemy.sql import func

from app.core.database import Base
from app.models.reseller import _string_enum

# SQLite only auto-increments an INTEGER PRIMARY KEY.
BIGINT_ID = BigInteger().with_variant(Integer(), "sqlite")

_FK_BILLING_ACCOUNTS = "billing_accounts.id"
_FK_TAX_CLASSES = "tax_classes.id"
_FK_TAX_RULES = "tax_rules.id"
_FK_USERS = "users.id"
_ON_DELETE_SET_NULL = "SET NULL"


class TaxClassCode(str, enum.Enum):
    """Codes of the seeded system classes; operators may add more."""

    STANDARD = "standard"
    REDUCED = "reduced"
    ZERO = "zero"
    EXEMPT = "exempt"
    OUTSIDE_SCOPE = "outside_scope"


class TaxCustomerType(str, enum.Enum):
    ANY = "any"
    BUSINESS = "business"
    CONSUMER = "consumer"


class TaxRoundingPoint(str, enum.Enum):
    LINE = "line"
    INVOICE_RATE = "invoice_rate"


class TaxRuleSource(str, enum.Enum):
    ADMIN = "admin"
    PACK = "pack"
    TAX_RATES = "tax_rates"


class TaxRegistrationScheme(str, enum.Enum):
    DOMESTIC = "domestic"
    OSS_UNION = "oss_union"
    OSS_NON_UNION = "oss_non_union"


class TaxExemptionScope(str, enum.Enum):
    ACCOUNT = "account"
    # Reserved for client groups (D5); refused in phase 2.
    CLIENT_GROUP = "client_group"


class TaxIdType(str, enum.Enum):
    EU_VAT = "eu_vat"
    GB_VAT = "gb_vat"
    OTHER = "other"


class TaxIdStatus(str, enum.Enum):
    UNCHECKED = "unchecked"
    VALID = "valid"
    INVALID = "invalid"
    UNAVAILABLE = "unavailable"
    FORMAT_INVALID = "format_invalid"


# Jurisdiction value of rules that need no seller registration.
JURISDICTION_ANY = "ANY"


class TaxClass(Base):
    __tablename__ = "tax_classes"

    id = Column(Integer, primary_key=True)
    code = Column(String(32), nullable=False, unique=True)
    name = Column(String(128), nullable=False)
    description = Column(String(512), nullable=True)
    system = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TaxRule(Base):
    """One tax rate rule. A rule whose ``valid_from`` has passed is never edited in place."""

    __tablename__ = "tax_rules"
    __table_args__ = (
        Index("ix_tax_rules_country_class_enabled", "country", "tax_class_id", "enabled"),
        Index("ix_tax_rules_pack_rule", "pack_id", "pack_rule_key"),
        UniqueConstraint("legacy_tax_rate_id", name="uq_tax_rules_legacy_tax_rate_id"),
    )

    id = Column(Integer, primary_key=True)
    name = Column(String(255), nullable=False)
    country = Column(String(2), nullable=True)
    region = Column(String(128), nullable=True)
    postcode_pattern = Column(String(128), nullable=True)
    tax_class_id = Column(
        Integer,
        ForeignKey(_FK_TAX_CLASSES, ondelete="RESTRICT", name="fk_tax_rules_tax_class_id"),
        nullable=False,
        index=True,
    )
    customer_type = Column(
        _string_enum(TaxCustomerType, length=16),
        nullable=False,
        default=TaxCustomerType.ANY,
        server_default=TaxCustomerType.ANY.value,
    )
    rate_percent = Column(Numeric(9, 4), nullable=False)
    priority = Column(Integer, nullable=False, default=0, server_default="0")
    compound = Column(Boolean, nullable=False, default=False, server_default="0")
    compound_order = Column(SmallInteger, nullable=False, default=0, server_default="0")
    jurisdiction = Column(
        String(16), nullable=False, default=JURISDICTION_ANY, server_default=JURISDICTION_ANY
    )
    reverse_charge = Column(Boolean, nullable=False, default=False, server_default="0")
    reverse_charge_zone = Column(String(16), nullable=True)
    legal_note_key = Column(String(64), nullable=True)
    rounding_point = Column(
        _string_enum(TaxRoundingPoint, length=16),
        nullable=False,
        default=TaxRoundingPoint.INVOICE_RATE,
        server_default=TaxRoundingPoint.INVOICE_RATE.value,
    )
    rounding_rule = Column(String(32), nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    source = Column(
        _string_enum(TaxRuleSource, length=16),
        nullable=False,
        default=TaxRuleSource.ADMIN,
        server_default=TaxRuleSource.ADMIN.value,
    )
    # "uk_vat", "eu_vat" or "<plugin_id>:<provider_id>".
    pack_id = Column(String(128), nullable=True)
    pack_version = Column(String(32), nullable=True)
    pack_rule_key = Column(String(128), nullable=True)
    legacy_tax_rate_id = Column(
        Integer,
        ForeignKey("tax_rates.id", ondelete=_ON_DELETE_SET_NULL, name="fk_tax_rules_legacy_tax_rate_id"),
        nullable=True,
    )
    in_effect_since = Column(DateTime(timezone=True), nullable=True)
    supersedes_id = Column(
        Integer,
        ForeignKey(_FK_TAX_RULES, ondelete=_ON_DELETE_SET_NULL, name="fk_tax_rules_supersedes_id"),
        nullable=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    tax_class = relationship("TaxClass")
    supersedes = relationship("TaxRule", remote_side=[id])


class InvoiceProfile(Base):
    """Document titles, footer and required buyer fields printed on invoices."""

    __tablename__ = "invoice_profiles"

    id = Column(Integer, primary_key=True)
    name = Column(String(128), nullable=False, unique=True)
    document_title = Column(String(128), nullable=False, default="Invoice", server_default="Invoice")
    proforma_title = Column(String(128), nullable=True)
    credit_note_title = Column(String(128), nullable=True)
    footer_text = Column(String(2000), nullable=True)
    locale = Column(String(16), nullable=False, default="en", server_default="en")
    required_buyer_fields = Column(JSON, nullable=True)
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class SellerTaxRegistration(Base):
    __tablename__ = "seller_tax_registrations"

    id = Column(Integer, primary_key=True)
    jurisdiction = Column(String(16), nullable=False, index=True)
    scheme = Column(_string_enum(TaxRegistrationScheme, length=16), nullable=False)
    tax_number = Column(String(64), nullable=False)
    country = Column(String(2), nullable=False)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    invoice_series_id = Column(
        Integer,
        ForeignKey(
            "document_series.id", ondelete="RESTRICT", name="fk_seller_tax_registrations_series_id"
        ),
        nullable=True,
    )
    invoice_profile_id = Column(
        Integer,
        ForeignKey(
            "invoice_profiles.id",
            ondelete=_ON_DELETE_SET_NULL,
            name="fk_seller_tax_registrations_profile_id",
        ),
        nullable=True,
    )
    enabled = Column(Boolean, nullable=False, default=True, server_default="1")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )

    invoice_profile = relationship("InvoiceProfile")


class TaxExemption(Base):
    """An exemption of one billing account; written only by the tax admin API."""

    __tablename__ = "tax_exemptions"

    id = Column(Integer, primary_key=True)
    scope = Column(
        _string_enum(TaxExemptionScope, length=16),
        nullable=False,
        default=TaxExemptionScope.ACCOUNT,
        server_default=TaxExemptionScope.ACCOUNT.value,
    )
    billing_account_id = Column(
        Integer,
        ForeignKey(_FK_BILLING_ACCOUNTS, ondelete="CASCADE", name="fk_tax_exemptions_billing_account_id"),
        nullable=False,
        index=True,
    )
    reason = Column(String(255), nullable=False)
    certificate_ref = Column(String(128), nullable=True)
    valid_from = Column(DateTime(timezone=True), nullable=False)
    valid_to = Column(DateTime(timezone=True), nullable=True)
    # Empty list: every class.
    tax_class_ids = Column(JSON, nullable=False, default=list)
    created_by_user_id = Column(
        Integer,
        ForeignKey(_FK_USERS, ondelete=_ON_DELETE_SET_NULL, name="fk_tax_exemptions_created_by_user_id"),
        nullable=True,
    )
    revoked_at = Column(DateTime(timezone=True), nullable=True)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TaxLegalNote(Base):
    """Text of a legal note key in one locale; copied into invoices at publish."""

    __tablename__ = "tax_legal_notes"
    __table_args__ = (UniqueConstraint("key", "locale", name="uq_tax_legal_notes_key_locale"),)

    id = Column(Integer, primary_key=True)
    key = Column(String(64), nullable=False)
    locale = Column(String(16), nullable=False)
    text = Column(String(1000), nullable=False)
    # Set when an admin edits the text, so a pack update keeps the edit (design 7.3).
    edited_by_admin = Column(Boolean, nullable=False, default=False, server_default="0")
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TaxIdCheck(Base):
    """Cache and evidence of one tax-ID check. Rows are only ever added."""

    __tablename__ = "tax_id_checks"
    __table_args__ = (
        Index(
            "ix_tax_id_checks_lookup", "country", "normalised_id", "validator", "checked_at"
        ),
    )

    id = Column(BIGINT_ID, primary_key=True)
    country = Column(String(2), nullable=False)
    vies_country = Column(String(2), nullable=True)
    normalised_id = Column(String(64), nullable=False)
    id_type = Column(String(16), nullable=False)
    # "core_format" or "<plugin_id>:<provider_id>".
    validator = Column(String(128), nullable=False)
    status = Column(String(16), nullable=False)
    checked_at = Column(DateTime(timezone=True), nullable=False)
    expires_at = Column(DateTime(timezone=True), nullable=False)
    request_ref = Column(String(64), nullable=True)
    registered_name = Column(String(512), nullable=True)
    registered_address = Column(String(512), nullable=True)
    error_code = Column(String(64), nullable=True)
    billing_account_id = Column(
        Integer,
        ForeignKey(
            _FK_BILLING_ACCOUNTS, ondelete=_ON_DELETE_SET_NULL, name="fk_tax_id_checks_billing_account_id"
        ),
        nullable=True,
        index=True,
    )
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class InvoiceLineTax(Base):
    """One tax applied to one invoice line; written only by ``InvoiceService``."""

    __tablename__ = "invoice_line_taxes"
    __table_args__ = (
        UniqueConstraint("invoice_line_id", "position", name="uq_invoice_line_taxes_line_position"),
    )

    id = Column(BIGINT_ID, primary_key=True)
    invoice_id = Column(
        Integer,
        ForeignKey("invoices.id", ondelete="CASCADE", name="fk_invoice_line_taxes_invoice_id"),
        nullable=False,
        index=True,
    )
    invoice_line_id = Column(
        Integer,
        ForeignKey("invoice_lines.id", ondelete="CASCADE", name="fk_invoice_line_taxes_line_id"),
        nullable=False,
    )
    tax_rule_id = Column(
        Integer,
        ForeignKey(_FK_TAX_RULES, ondelete="RESTRICT", name="fk_invoice_line_taxes_tax_rule_id"),
        nullable=True,
        index=True,
    )
    position = Column(SmallInteger, nullable=False, default=0, server_default="0")
    name = Column(String(255), nullable=False)
    rate_percent = Column(Numeric(9, 4), nullable=False)
    jurisdiction = Column(String(16), nullable=False)
    base_cents = Column(BigInteger, nullable=False)
    tax_cents = Column(BigInteger, nullable=False)
    reverse_charge = Column(Boolean, nullable=False, default=False, server_default="0")
    legal_note_key = Column(String(64), nullable=True)
    # "rules", "tax_rates" or "provider:<plugin_id>:<provider_id>".
    source = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class InvoiceAdjustmentLineTax(Base):
    """One tax row of a credit or debit note line; written only by ``InvoiceAdjustmentService``."""

    __tablename__ = "invoice_adjustment_line_taxes"
    __table_args__ = (
        UniqueConstraint(
            "invoice_adjustment_line_id", "position", name="uq_invoice_adjustment_line_taxes_line_position"
        ),
    )

    id = Column(BIGINT_ID, primary_key=True)
    invoice_adjustment_id = Column(
        Integer,
        ForeignKey(
            "invoice_adjustments.id", ondelete="RESTRICT", name="fk_invoice_adjustment_line_taxes_adjustment_id"
        ),
        nullable=False,
        index=True,
    )
    invoice_adjustment_line_id = Column(
        Integer,
        ForeignKey(
            "invoice_adjustment_lines.id", ondelete="RESTRICT", name="fk_invoice_adjustment_line_taxes_line_id"
        ),
        nullable=False,
    )
    tax_rule_id = Column(
        Integer,
        ForeignKey(_FK_TAX_RULES, ondelete="RESTRICT", name="fk_invoice_adjustment_line_taxes_tax_rule_id"),
        nullable=True,
        index=True,
    )
    source_line_tax_id = Column(
        BIGINT_ID,
        ForeignKey(
            "invoice_line_taxes.id", ondelete="RESTRICT", name="fk_invoice_adjustment_line_taxes_source_id"
        ),
        nullable=True,
        index=True,
    )
    position = Column(SmallInteger, nullable=False, default=0, server_default="0")
    name = Column(String(255), nullable=False)
    rate_percent = Column(Numeric(9, 4), nullable=False)
    jurisdiction = Column(String(16), nullable=False)
    base_cents = Column(BigInteger, nullable=False)
    tax_cents = Column(BigInteger, nullable=False)
    reverse_charge = Column(Boolean, nullable=False, default=False, server_default="0")
    legal_note_key = Column(String(64), nullable=True)
    # "rules", "tax_rates" or "provider:<plugin_id>:<provider_id>".
    source = Column(String(128), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
    updated_at = Column(
        DateTime(timezone=True), server_default=func.now(), onupdate=func.now(), nullable=False
    )


class TaxExchangeRate(Base):
    """A published exchange rate for one month (design 4.10): ``units_per_gbp`` units of
    ``currency`` buy one unit of ``base``. Rows are written once and never updated."""

    __tablename__ = "tax_exchange_rates"
    __table_args__ = (
        UniqueConstraint("source", "base", "currency", "period", name="uq_tax_exchange_rates_source_base_currency_period"),
    )

    id = Column(Integer, primary_key=True)
    source = Column(String(32), nullable=False)
    base = Column(String(3), nullable=False, default="GBP", server_default="GBP")
    currency = Column(String(3), nullable=False)
    period = Column(String(7), nullable=False, index=True)
    units_per_gbp = Column(Numeric(18, 6), nullable=False)
    period_start = Column(Date, nullable=False)
    period_end = Column(Date, nullable=False)
    fetched_at = Column(DateTime(timezone=True), nullable=False)
    created_at = Column(DateTime(timezone=True), server_default=func.now(), nullable=False)
