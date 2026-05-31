from __future__ import annotations

from copy import deepcopy
from typing import Any

from sqlalchemy.orm import Session

from app.models.product_catalog import Product, ProductFamily
from app.models.vm_config import FamilyVMConfig, ProductVMConfig


def _deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    result = deepcopy(base)
    for key, value in (override or {}).items():
        if isinstance(value, dict) and isinstance(result.get(key), dict):
            result[key] = _deep_merge(result[key], value)
        else:
            result[key] = value
    return result


class FamilyVMConfigDAO:
    @staticmethod
    def get_by_family_id(db: Session, family_id: int) -> FamilyVMConfig | None:
        return db.query(FamilyVMConfig).filter(FamilyVMConfig.family_id == family_id).first()

    @staticmethod
    def upsert(db: Session, family: ProductFamily, config: dict[str, Any]) -> FamilyVMConfig:
        row = FamilyVMConfigDAO.get_by_family_id(db, family.id)
        if row is None:
            row = FamilyVMConfig(family_id=family.id, config=config or {})
            db.add(row)
        else:
            row.config = config or {}
        db.flush()
        return row


class ProductVMConfigDAO:
    @staticmethod
    def get_by_product_id(db: Session, product_id: int) -> ProductVMConfig | None:
        return db.query(ProductVMConfig).filter(ProductVMConfig.product_id == product_id).first()

    @staticmethod
    def upsert(
        db: Session,
        product: Product,
        extends_family: bool,
        config: dict[str, Any],
    ) -> ProductVMConfig:
        row = ProductVMConfigDAO.get_by_product_id(db, product.id)
        if row is None:
            row = ProductVMConfig(
                product_id=product.id,
                extends_family=extends_family,
                config=config or {},
            )
            db.add(row)
        else:
            row.extends_family = extends_family
            row.config = config or {}
        db.flush()
        return row

    @staticmethod
    def resolve_effective_config(db: Session, product: Product) -> dict[str, Any]:
        product_cfg = ProductVMConfigDAO.get_by_product_id(db, product.id)
        family_cfg = None
        if product.family_id:
            family_cfg = FamilyVMConfigDAO.get_by_family_id(db, product.family_id)

        family_data = (family_cfg.config if family_cfg else {}) or {}
        product_data = (product_cfg.config if product_cfg else {}) or {}

        if product_cfg is None:
            return family_data
        if product_cfg.extends_family:
            return _deep_merge(family_data, product_data)
        return product_data
