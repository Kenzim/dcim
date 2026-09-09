"""Database queries for billing accounts, profiles, and system settings."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy import or_, select
from sqlalchemy.orm import Session, joinedload

from app.models.commerce_account import (
    BillingAccount,
    BillingAccountStatus,
    BillingAccountType,
    BillingProfile,
    SystemSetting,
)
from app.models.reseller import Reseller
from app.models.user import User


class BillingAccountDAO:
    @staticmethod
    def get(db: Session, account_id: int) -> Optional[BillingAccount]:
        return db.execute(
            select(BillingAccount).where(BillingAccount.id == account_id)
        ).scalar_one_or_none()

    @staticmethod
    def get_for_update(db: Session, account_id: int) -> Optional[BillingAccount]:
        return db.execute(
            select(BillingAccount)
            .where(BillingAccount.id == account_id)
            .with_for_update()
        ).scalar_one_or_none()

    @staticmethod
    def get_by_user_id(db: Session, user_id: int) -> Optional[BillingAccount]:
        return db.execute(
            select(BillingAccount).where(
                BillingAccount.account_type == BillingAccountType.CLIENT,
                BillingAccount.user_id == user_id,
            )
        ).scalar_one_or_none()

    @staticmethod
    def get_by_reseller_id(db: Session, reseller_id: int) -> Optional[BillingAccount]:
        return db.execute(
            select(BillingAccount).where(
                BillingAccount.account_type == BillingAccountType.RESELLER,
                BillingAccount.reseller_id == reseller_id,
            )
        ).scalar_one_or_none()

    @staticmethod
    def ensure_client_account(
        db: Session,
        user_id: int,
        *,
        currency: str = "USD",
    ) -> BillingAccount:
        existing = BillingAccountDAO.get_by_user_id(db, user_id)
        if existing is not None:
            return existing
        account = BillingAccount(
            account_type=BillingAccountType.CLIENT,
            user_id=user_id,
            currency=currency.strip().upper()[:3],
            status=BillingAccountStatus.ACTIVE,
        )
        db.add(account)
        db.flush()
        return account

    @staticmethod
    def ensure_reseller_account(db: Session, reseller: Reseller) -> BillingAccount:
        existing = BillingAccountDAO.get_by_reseller_id(db, reseller.id)
        if existing is not None:
            return existing
        account = BillingAccount(
            account_type=BillingAccountType.RESELLER,
            reseller_id=reseller.id,
            user_id=reseller.user_id,
            currency="USD",
            status=BillingAccountStatus.ACTIVE,
            credit_balance_cents=int(reseller.cached_balance_cents or 0),
        )
        db.add(account)
        db.flush()
        return account

    @staticmethod
    def list_for_admin(
        db: Session,
        *,
        status: Optional[BillingAccountStatus] = None,
        q: Optional[str] = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[BillingAccount]:
        stmt = (
            select(BillingAccount)
            .options(
                joinedload(BillingAccount.user),
                joinedload(BillingAccount.reseller).joinedload(Reseller.user),
                joinedload(BillingAccount.profile),
            )
            .order_by(BillingAccount.id.desc())
        )
        if status is not None:
            stmt = stmt.where(BillingAccount.status == status)
        if q:
            needle = f"%{q.strip()}%"
            stmt = stmt.outerjoin(User, BillingAccount.user_id == User.id).outerjoin(
                Reseller, BillingAccount.reseller_id == Reseller.id
            ).where(
                or_(
                    User.username.ilike(needle),
                    User.email.ilike(needle),
                    Reseller.name.ilike(needle),
                )
            )
        return list(db.execute(stmt.offset(offset).limit(limit)).unique().scalars())


class BillingProfileDAO:
    @staticmethod
    def get_by_account(db: Session, billing_account_id: int) -> Optional[BillingProfile]:
        return db.execute(
            select(BillingProfile).where(
                BillingProfile.billing_account_id == billing_account_id
            )
        ).scalar_one_or_none()

    @staticmethod
    def upsert(
        db: Session,
        billing_account_id: int,
        **fields: Any,
    ) -> BillingProfile:
        profile = BillingProfileDAO.get_by_account(db, billing_account_id)
        if profile is None:
            profile = BillingProfile(billing_account_id=billing_account_id)
            db.add(profile)
        for key, value in fields.items():
            if hasattr(profile, key):
                setattr(profile, key, value)
        db.flush()
        return profile


class SystemSettingDAO:
    @staticmethod
    def get(db: Session, key: str) -> Optional[dict]:
        row = db.get(SystemSetting, key)
        if row is None:
            return None
        value = row.value
        return dict(value) if isinstance(value, dict) else value

    @staticmethod
    def set(db: Session, key: str, value: Any) -> SystemSetting:
        row = db.get(SystemSetting, key)
        if row is None:
            row = SystemSetting(key=key, value=value if isinstance(value, dict) else {"value": value})
            db.add(row)
        else:
            row.value = value if isinstance(value, dict) else {"value": value}
        db.flush()
        return row
