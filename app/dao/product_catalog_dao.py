from typing import List, Optional

from sqlalchemy.orm import Session

from app.models.product_catalog import (
    ProductFamily,
    Product,
    OSProfile,
    ProductFamilyOSProfile,
    VMTemplate,
    ProductVMTemplate,
)


class ProductFamilyDAO:
    @staticmethod
    def create(
        db: Session,
        name: str,
        description: Optional[str],
        code: str,
        service_type: str,
        provisioning_backend: Optional[str] = None,
        defaults: Optional[dict] = None,
        constraints: Optional[dict] = None,
        enabled: bool = True,
    ) -> ProductFamily:
        row = ProductFamily(
            name=name,
            description=description,
            code=code,
            service_type=service_type,
            provisioning_backend=provisioning_backend,
            defaults=defaults or {},
            constraints=constraints or {},
            enabled=enabled,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, family_id: int) -> Optional[ProductFamily]:
        return db.query(ProductFamily).filter(ProductFamily.id == family_id).first()

    @staticmethod
    def get_by_code(db: Session, code: str) -> Optional[ProductFamily]:
        return db.query(ProductFamily).filter(ProductFamily.code == code).first()

    @staticmethod
    def get_all(db: Session) -> List[ProductFamily]:
        return db.query(ProductFamily).order_by(ProductFamily.name).all()

    @staticmethod
    def update(db: Session, row: ProductFamily, **kwargs) -> ProductFamily:
        for key, value in kwargs.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, family_id: int) -> bool:
        row = ProductFamilyDAO.get_by_id(db, family_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True


class ProductDAO:
    @staticmethod
    def create(
        db: Session,
        family_id: Optional[int],
        name: str,
        description: Optional[str],
        code: str,
        overrides: Optional[dict] = None,
        enabled: bool = True,
    ) -> Product:
        row = Product(
            family_id=family_id,
            name=name,
            description=description,
            code=code,
            overrides=overrides or {},
            enabled=enabled,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, product_id: int) -> Optional[Product]:
        return db.query(Product).filter(Product.id == product_id).first()

    @staticmethod
    def get_by_code(db: Session, code: str) -> Optional[Product]:
        return db.query(Product).filter(Product.code == code).first()

    @staticmethod
    def get_all(db: Session) -> List[Product]:
        return db.query(Product).order_by(Product.name).all()

    @staticmethod
    def get_ungrouped(db: Session) -> List[Product]:
        return (
            db.query(Product)
            .filter(Product.family_id.is_(None))
            .order_by(Product.name)
            .all()
        )

    @staticmethod
    def update(db: Session, row: Product, **kwargs) -> Product:
        for key, value in kwargs.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def set_vm_templates(db: Session, product: Product, vm_template_ids: list[int]) -> None:
        db.query(ProductVMTemplate).filter(ProductVMTemplate.product_id == product.id).delete()
        unique_ids = sorted(set(vm_template_ids or []))
        for template_id in unique_ids:
            db.add(ProductVMTemplate(product_id=product.id, vm_template_id=template_id))
        db.flush()

    @staticmethod
    def delete(db: Session, product_id: int) -> bool:
        row = ProductDAO.get_by_id(db, product_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True

    @staticmethod
    def product_has_vm_template(db: Session, product: Product, vm_template_id: int) -> bool:
        return (
            db.query(ProductVMTemplate)
            .filter(
                ProductVMTemplate.product_id == product.id,
                ProductVMTemplate.vm_template_id == vm_template_id,
            )
            .first()
            is not None
        )


class OSProfileDAO:
    @staticmethod
    def create(
        db: Session,
        code: str,
        name: str,
        os_family: str,
        strategy_name: Optional[str] = None,
        strategy_config: Optional[dict] = None,
        enabled: bool = True,
    ) -> OSProfile:
        row = OSProfile(
            code=code,
            name=name,
            os_family=os_family,
            strategy_name=strategy_name,
            strategy_config=strategy_config or {},
            enabled=enabled,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, os_profile_id: int) -> Optional[OSProfile]:
        return db.query(OSProfile).filter(OSProfile.id == os_profile_id).first()

    @staticmethod
    def get_by_code(db: Session, code: str) -> Optional[OSProfile]:
        return db.query(OSProfile).filter(OSProfile.code == code).first()

    @staticmethod
    def get_all(db: Session) -> List[OSProfile]:
        return db.query(OSProfile).order_by(OSProfile.name).all()

    @staticmethod
    def update(db: Session, row: OSProfile, **kwargs) -> OSProfile:
        for key, value in kwargs.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, os_profile_id: int) -> bool:
        row = OSProfileDAO.get_by_id(db, os_profile_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True


class ProductFamilyOSProfileDAO:
    @staticmethod
    def attach(db: Session, family_id: int, os_profile_id: int) -> ProductFamilyOSProfile:
        existing = (
            db.query(ProductFamilyOSProfile)
            .filter(
                ProductFamilyOSProfile.family_id == family_id,
                ProductFamilyOSProfile.os_profile_id == os_profile_id,
            )
            .first()
        )
        if existing:
            return existing
        row = ProductFamilyOSProfile(family_id=family_id, os_profile_id=os_profile_id)
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def detach(db: Session, family_id: int, os_profile_id: int) -> bool:
        row = (
            db.query(ProductFamilyOSProfile)
            .filter(
                ProductFamilyOSProfile.family_id == family_id,
                ProductFamilyOSProfile.os_profile_id == os_profile_id,
            )
            .first()
        )
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True


class VMTemplateDAO:
    @staticmethod
    def create(
        db: Session,
        name: str,
        os_type: str,
        proxmox_template_name: str,
        description: Optional[str] = None,
        enabled: bool = True,
    ) -> VMTemplate:
        row = VMTemplate(
            name=name,
            os_type=os_type,
            proxmox_template_name=proxmox_template_name,
            description=description,
            enabled=enabled,
        )
        db.add(row)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def get_by_id(db: Session, vm_template_id: int) -> Optional[VMTemplate]:
        return db.query(VMTemplate).filter(VMTemplate.id == vm_template_id).first()

    @staticmethod
    def get_by_proxmox_name(db: Session, proxmox_template_name: str) -> Optional[VMTemplate]:
        return db.query(VMTemplate).filter(VMTemplate.proxmox_template_name == proxmox_template_name).first()

    @staticmethod
    def get_all(db: Session) -> List[VMTemplate]:
        return db.query(VMTemplate).order_by(VMTemplate.name).all()

    @staticmethod
    def update(db: Session, row: VMTemplate, **kwargs) -> VMTemplate:
        for key, value in kwargs.items():
            setattr(row, key, value)
        db.commit()
        db.refresh(row)
        return row

    @staticmethod
    def delete(db: Session, vm_template_id: int) -> bool:
        row = VMTemplateDAO.get_by_id(db, vm_template_id)
        if not row:
            return False
        db.delete(row)
        db.commit()
        return True
