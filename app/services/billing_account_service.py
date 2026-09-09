"""Billing account ensure helpers for commerce clients and resellers."""

from __future__ import annotations

from typing import Any, Optional

from sqlalchemy.orm import Session

from app.dao.commerce_dao import BillingAccountDAO, BillingProfileDAO
from app.models.commerce_account import BillingAccount, BillingProfile
from app.models.reseller import Reseller


class BillingAccountService:
    @staticmethod
    def ensure_client(
        db: Session,
        user_id: int,
        *,
        currency: str = "USD",
    ) -> BillingAccount:
        return BillingAccountDAO.ensure_client_account(
            db, user_id, currency=currency
        )

    @staticmethod
    def ensure_reseller(db: Session, reseller: Reseller) -> BillingAccount:
        return BillingAccountDAO.ensure_reseller_account(db, reseller)

    @staticmethod
    def get(db: Session, account_id: int) -> Optional[BillingAccount]:
        return BillingAccountDAO.get(db, account_id)

    @staticmethod
    def get_profile(
        db: Session, billing_account_id: int
    ) -> Optional[BillingProfile]:
        return BillingProfileDAO.get_by_account(db, billing_account_id)

    @staticmethod
    def upsert_profile(
        db: Session, billing_account_id: int, **fields: Any
    ) -> BillingProfile:
        return BillingProfileDAO.upsert(db, billing_account_id, **fields)
