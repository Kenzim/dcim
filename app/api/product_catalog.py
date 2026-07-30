from typing import Any, Optional
import re

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.auth import require_admin
from app.core.database import get_db
from app.dao.product_catalog_dao import ProductFamilyDAO, ProductDAO, OSProfileDAO, ProductFamilyOSProfileDAO, VMTemplateDAO
from app.dao.vm_config_dao import FamilyVMConfigDAO, ProductVMConfigDAO
from app.dao.permission_set_dao import PermissionSetDAO
from app.models.service import Service, ServiceStatus
from app.services.vm_install_type_strategy import INSTALL_TYPE_STRATEGIES, list_os_type_schemas


router = APIRouter(prefix="/product-catalog", tags=["product-catalog"])
# VM template os_type values are the provisioning strategy keys (model + strategy merged).
ALLOWED_VM_OS_TYPES = sorted(INSTALL_TYPE_STRATEGIES.keys())
# Service types this catalog can define families/products for. bare_metal
# isn't included: those products are still ad hoc (server_group driven) and
# don't have a dedicated catalog UI yet.
ALLOWED_FAMILY_SERVICE_TYPES = ("vm", "http_proxy")


def _slugify(text: str) -> str:
    value = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return value or "family"


def _generate_family_code(db: Session, name: str) -> str:
    base = _slugify(name)
    candidate = base
    suffix = 2
    while ProductFamilyDAO.get_by_code(db, candidate):
        candidate = f"{base}-{suffix}"
        suffix += 1
    return candidate


class ProductFamilyCreate(BaseModel):
    name: str
    code: Optional[str] = None
    service_type: str = "vm"
    provisioning_backend: str = "proxmox"
    defaults: dict[str, Any] = Field(default_factory=dict)
    constraints: dict[str, Any] = Field(default_factory=dict)
    description: Optional[str] = None
    enabled: bool = True


class ProductFamilyUpdate(BaseModel):
    name: Optional[str] = None
    description: Optional[str] = None
    provisioning_backend: Optional[str] = None
    defaults: Optional[dict[str, Any]] = None
    constraints: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None


class ProductCreate(BaseModel):
    family_id: Optional[int] = None
    name: str
    description: Optional[str] = None
    code: str
    vm_template_ids: list[int] = Field(default_factory=list)
    overrides: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True
    permission_set_id: Optional[int] = Field(
        None, description="Client permission preset applied to services created from this product"
    )


class ProductUpdate(BaseModel):
    family_id: Optional[int] = None
    name: Optional[str] = None
    description: Optional[str] = None
    code: Optional[str] = None
    vm_template_ids: Optional[list[int]] = None
    overrides: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None
    permission_set_id: Optional[int] = Field(
        None, description="Client permission preset applied to services created from this product; null clears it"
    )


class FamilyVMConfigUpsert(BaseModel):
    config: dict[str, Any] = Field(default_factory=dict)


class ProductVMConfigUpsert(BaseModel):
    extends_family: bool = True
    config: dict[str, Any] = Field(default_factory=dict)


_VM_TEMPLATE_CODE_RE = re.compile(r"^[a-z0-9]([a-z0-9-]{0,126}[a-z0-9])?$")


def _validate_vm_template_code(code: str) -> str:
    value = (code or "").strip().lower()
    if not _VM_TEMPLATE_CODE_RE.match(value):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "code must be 1–128 chars, lowercase letters/digits/hyphens, "
                "and must start and end with a letter or digit"
            ),
        )
    return value


class VMTemplateCreate(BaseModel):
    code: str
    name: str
    os_type: str
    proxmox_template_name: str
    description: Optional[str] = None
    enabled: bool = True
    shared_storage: bool = False
    strategy_options: dict[str, Any] = Field(default_factory=dict)


class VMTemplateUpdate(BaseModel):
    name: Optional[str] = None
    os_type: Optional[str] = None
    proxmox_template_name: Optional[str] = None
    description: Optional[str] = None
    enabled: Optional[bool] = None
    shared_storage: Optional[bool] = None
    strategy_options: Optional[dict[str, Any]] = None


class OSProfileCreate(BaseModel):
    code: str
    name: str
    os_family: str
    strategy_name: Optional[str] = None
    strategy_config: dict[str, Any] = Field(default_factory=dict)
    enabled: bool = True


class OSProfileUpdate(BaseModel):
    name: Optional[str] = None
    os_family: Optional[str] = None
    strategy_name: Optional[str] = None
    strategy_config: Optional[dict[str, Any]] = None
    enabled: Optional[bool] = None


@router.get("/families")
async def list_families(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    families = ProductFamilyDAO.get_all(db)
    result = []
    for f in families:
        vm_row = FamilyVMConfigDAO.get_by_family_id(db, f.id)
        result.append(
            {
                "id": f.id,
                "name": f.name,
                "description": f.description,
                "code": f.code,
                "service_type": f.service_type,
                "provisioning_backend": f.provisioning_backend,
                "defaults": f.defaults,
                "constraints": f.constraints,
                "enabled": f.enabled,
                "vm_config": (vm_row.config if vm_row else {}),
                "products": [{"id": p.id, "code": p.code, "name": p.name, "enabled": p.enabled} for p in f.products],
                "os_profiles": [{"id": m.os_profile.id, "code": m.os_profile.code, "name": m.os_profile.name} for m in f.os_mappings],
            }
        )
    return result


def _validate_proxy_defaults(defaults: dict, db: Session | None = None) -> None:
    """Light validation for the http_proxy family/product ``defaults``/
    ``overrides`` JSON blob: ip_count/subnet_id/subnet_group_id/location_id,
    when present, must be sane. Everything else in the dict is passed through
    untouched."""
    if "ip_count" in defaults and defaults["ip_count"] is not None:
        try:
            ip_count = int(defaults["ip_count"])
        except (TypeError, ValueError):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="defaults.ip_count must be an integer")
        if ip_count < 1 or ip_count > 32:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="defaults.ip_count must be between 1 and 32")
    for key in ("subnet_id", "location_id", "subnet_group_id"):
        if key in defaults and defaults[key] is not None:
            try:
                int(defaults[key])
            except (TypeError, ValueError):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"defaults.{key} must be an integer")
    if "allocation_strategy" in defaults and defaults["allocation_strategy"] is not None:
        if not isinstance(defaults["allocation_strategy"], str):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="defaults.allocation_strategy must be a string")
    if db is not None and defaults.get("subnet_group_id") is not None:
        from app.dao.proxy_subnet_group_dao import ProxySubnetGroupDAO

        group = ProxySubnetGroupDAO.get_by_id(db, int(defaults["subnet_group_id"]))
        if not group:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="defaults.subnet_group_id does not refer to an existing proxy subnet group",
            )


@router.post("/families", status_code=status.HTTP_201_CREATED)
async def create_family(
    data: ProductFamilyCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if data.service_type not in ALLOWED_FAMILY_SERVICE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"service_type must be one of {list(ALLOWED_FAMILY_SERVICE_TYPES)}",
        )

    payload = data.model_dump()
    payload["code"] = data.code or _generate_family_code(db, data.name)

    if data.service_type == "vm":
        if data.provisioning_backend not in ("proxmox", ""):
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="VM product families must use provisioning_backend 'proxmox'",
            )
        payload["provisioning_backend"] = "proxmox"
    else:
        # http_proxy: no hardware/hypervisor backend — IPs come from IPAM.
        _validate_proxy_defaults(data.defaults or {}, db=db)
        payload["provisioning_backend"] = data.provisioning_backend or "ipam"

    if ProductFamilyDAO.get_by_code(db, payload["code"]):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Family code already exists")

    family = ProductFamilyDAO.create(db, **payload)
    return {"id": family.id}


@router.put("/families/{family_id}")
async def update_family(
    family_id: int,
    data: ProductFamilyUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProductFamilyDAO.get_by_id(db, family_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Family not found")
    if row.service_type not in ALLOWED_FAMILY_SERVICE_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Only VM/http_proxy families are editable from this catalog",
        )
    update_data = data.model_dump(exclude_unset=True)
    if row.service_type == "vm":
        if "provisioning_backend" in update_data:
            backend = update_data["provisioning_backend"]
            if backend not in (None, "", "proxmox"):
                raise HTTPException(
                    status_code=status.HTTP_400_BAD_REQUEST,
                    detail="VM product families must use provisioning_backend 'proxmox'",
                )
            update_data["provisioning_backend"] = "proxmox"
    else:
        if "defaults" in update_data and update_data["defaults"] is not None:
            _validate_proxy_defaults(update_data["defaults"], db=db)
    ProductFamilyDAO.update(db, row, **update_data)
    return {"status": "ok"}


@router.post("/families/{family_id}/bulk-defaults")
async def bulk_update_family_defaults(
    family_id: int,
    defaults: dict[str, Any],
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProductFamilyDAO.get_by_id(db, family_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Family not found")
    merged = dict(row.defaults or {})
    merged.update(defaults or {})
    ProductFamilyDAO.update(db, row, defaults=merged)
    return {"status": "ok", "defaults": merged}


@router.post("/products", status_code=status.HTTP_201_CREATED)
async def create_product(
    data: ProductCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if data.family_id is not None:
        family = ProductFamilyDAO.get_by_id(db, data.family_id)
        if not family:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Family not found")
        if family.service_type == "http_proxy":
            _validate_proxy_defaults(data.overrides or {}, db=db)
    if data.permission_set_id is not None and PermissionSetDAO.get_by_id(db, data.permission_set_id) is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")
    if ProductDAO.get_by_code(db, data.code):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Product code already exists")
    payload = data.model_dump()
    vm_template_ids = payload.pop("vm_template_ids", [])
    if vm_template_ids:
        valid_ids = {row.id for row in VMTemplateDAO.get_all(db)}
        missing = sorted(set(vm_template_ids) - valid_ids)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown vm_template_ids: {missing}",
            )
    row = ProductDAO.create(db, **payload)
    if vm_template_ids:
        ProductDAO.set_vm_templates(db, row, vm_template_ids)
        db.commit()
    return {"id": row.id}


@router.get("/products")
async def list_products(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = ProductDAO.get_all(db)
    result = []
    for p in rows:
        vm_row = ProductVMConfigDAO.get_by_product_id(db, p.id)
        result.append(
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "code": p.code,
                "family_id": p.family_id,
                "family_code": p.family.code if p.family else None,
                "family_service_type": (p.family.service_type if p.family else None),
                "vm_template_ids": [m.vm_template_id for m in p.vm_template_mappings],
                "overrides": p.overrides or {},
                "enabled": p.enabled,
                "vm_config": (vm_row.config if vm_row else {}),
                "extends_group_vm_config": (vm_row.extends_family if vm_row else True),
                "effective_vm_config": ProductVMConfigDAO.resolve_effective_config(db, p),
                "permission_set_id": p.permission_set_id,
                "permission_set_name": p.permission_set.name if p.permission_set else None,
            }
        )
    return result


@router.put("/products/{product_id}")
async def update_product(
    product_id: int,
    data: ProductUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProductDAO.get_by_id(db, product_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    update_data = data.model_dump(exclude_unset=True)
    if "family_id" in update_data and update_data["family_id"] is not None:
        family = ProductFamilyDAO.get_by_id(db, update_data["family_id"])
        if not family:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Family not found")
    if "overrides" in update_data and update_data["overrides"] is not None:
        target_family = row.family
        if "family_id" in update_data and update_data["family_id"] is not None:
            target_family = ProductFamilyDAO.get_by_id(db, update_data["family_id"])
        if target_family and target_family.service_type == "http_proxy":
            _validate_proxy_defaults(update_data["overrides"], db=db)
    if "permission_set_id" in update_data and update_data["permission_set_id"] is not None:
        if PermissionSetDAO.get_by_id(db, update_data["permission_set_id"]) is None:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Permission set not found")
    if "code" in update_data:
        existing = ProductDAO.get_by_code(db, update_data["code"])
        if existing and existing.id != row.id:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Product code already exists")
    vm_template_ids = update_data.pop("vm_template_ids", None)
    if vm_template_ids is not None:
        valid_ids = {tmpl.id for tmpl in VMTemplateDAO.get_all(db)}
        missing = sorted(set(vm_template_ids) - valid_ids)
        if missing:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail=f"Unknown vm_template_ids: {missing}",
            )

    ProductDAO.update(db, row, **update_data)
    if vm_template_ids is not None:
        ProductDAO.set_vm_templates(db, row, vm_template_ids)
        db.commit()
    return {"status": "ok"}


@router.delete("/products/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_product(
    product_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = ProductDAO.get_by_id(db, product_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")

    in_use = (
        db.query(Service)
        .filter(Service.product_code == row.code, Service.status != ServiceStatus.TERMINATED)
        .count()
    )
    if in_use:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=(
                f"Product '{row.code}' is still linked to {in_use} non-terminated service(s). "
                "Disable the product instead, or reassign/terminate those services first."
            ),
        )

    ProductDAO.delete(db, product_id)
    return None


@router.get("/vm-templates/os-types")
async def list_vm_template_os_types(
    auth: dict = Depends(require_admin),
    detailed: bool = False,
):
    """Return allowed os_type strings, or detailed strategy schemas when ``detailed=true``."""
    if detailed:
        return list_os_type_schemas()
    return ALLOWED_VM_OS_TYPES


@router.get("/vm-templates")
async def list_vm_templates(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    from app.services.ssh_public_keys import os_type_accepts_ssh_key

    rows = VMTemplateDAO.get_all(db)
    return [
        {
            "id": t.id,
            "code": t.code,
            "name": t.name,
            "description": t.description,
            "os_type": t.os_type,
            "proxmox_template_name": t.proxmox_template_name,
            "strategy_options": t.strategy_options or {},
            "enabled": t.enabled,
            "shared_storage": bool(t.shared_storage),
            "product_ids": [m.product_id for m in t.product_mappings],
            "accepts_ssh_key": os_type_accepts_ssh_key(t.os_type),
        }
        for t in rows
    ]


@router.post("/vm-templates", status_code=status.HTTP_201_CREATED)
async def create_vm_template(
    data: VMTemplateCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if data.os_type not in ALLOWED_VM_OS_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported os_type '{data.os_type}'",
        )
    code = _validate_vm_template_code(data.code)
    if VMTemplateDAO.get_by_code(db, code):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="code already exists",
        )
    if VMTemplateDAO.get_by_proxmox_name(db, data.proxmox_template_name):
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="proxmox_template_name already exists",
        )
    payload = data.model_dump()
    payload["code"] = code
    row = VMTemplateDAO.create(db, **payload)
    return {"id": row.id, "code": row.code}


@router.put("/vm-templates/{template_id}")
async def update_vm_template(
    template_id: int,
    data: VMTemplateUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = VMTemplateDAO.get_by_id(db, template_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM template not found")
    update_data = data.model_dump(exclude_unset=True)
    if "code" in update_data:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="code is immutable after create",
        )
    if "os_type" in update_data and update_data["os_type"] not in ALLOWED_VM_OS_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=f"Unsupported os_type '{update_data['os_type']}'",
        )
    if "proxmox_template_name" in update_data:
        existing = VMTemplateDAO.get_by_proxmox_name(db, update_data["proxmox_template_name"])
        if existing and existing.id != row.id:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="proxmox_template_name already exists",
            )
    VMTemplateDAO.update(db, row, **update_data)
    return {"status": "ok"}


@router.delete("/vm-templates/{template_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_vm_template(
    template_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if not VMTemplateDAO.delete(db, template_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="VM template not found")
    return None


@router.get("/families/{family_id}/vm-config")
async def get_family_vm_config(
    family_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    family = ProductFamilyDAO.get_by_id(db, family_id)
    if not family:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Family not found")
    row = FamilyVMConfigDAO.get_by_family_id(db, family_id)
    return {
        "family_id": family_id,
        "config": (row.config if row else {}),
    }


@router.put("/families/{family_id}/vm-config")
async def upsert_family_vm_config(
    family_id: int,
    data: FamilyVMConfigUpsert,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    family = ProductFamilyDAO.get_by_id(db, family_id)
    if not family:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Family not found")
    if family.service_type != "vm":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="vm-config only applies to VM families")
    FamilyVMConfigDAO.upsert(db, family, data.config or {})
    db.commit()
    return {"status": "ok"}


@router.get("/products/{product_id}/vm-config")
async def get_product_vm_config(
    product_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = ProductDAO.get_by_id(db, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    row = ProductVMConfigDAO.get_by_product_id(db, product_id)
    return {
        "product_id": product_id,
        "extends_family": (row.extends_family if row else True),
        "config": (row.config if row else {}),
        "effective_config": ProductVMConfigDAO.resolve_effective_config(db, product),
    }


@router.put("/products/{product_id}/vm-config")
async def upsert_product_vm_config(
    product_id: int,
    data: ProductVMConfigUpsert,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    product = ProductDAO.get_by_id(db, product_id)
    if not product:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Product not found")
    if not product.family or product.family.service_type != "vm":
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="vm-config only applies to VM products")
    ProductVMConfigDAO.upsert(
        db,
        product=product,
        extends_family=data.extends_family,
        config=data.config or {},
    )
    db.commit()
    return {"status": "ok"}


@router.get("/os-profiles")
async def list_os_profiles(
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    rows = OSProfileDAO.get_all(db)
    return [
        {
            "id": r.id,
            "code": r.code,
            "name": r.name,
            "os_family": r.os_family,
            "strategy_name": r.strategy_name,
            "enabled": r.enabled,
        }
        for r in rows
    ]


@router.post("/os-profiles", status_code=status.HTTP_201_CREATED)
async def create_os_profile(
    data: OSProfileCreate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    if OSProfileDAO.get_by_code(db, data.code):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="OS profile code already exists")
    row = OSProfileDAO.create(db, **data.model_dump())
    return {"id": row.id}


@router.put("/os-profiles/{os_profile_id}")
async def update_os_profile(
    os_profile_id: int,
    data: OSProfileUpdate,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    row = OSProfileDAO.get_by_id(db, os_profile_id)
    if not row:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="OS profile not found")
    OSProfileDAO.update(db, row, **data.model_dump(exclude_unset=True))
    return {"status": "ok"}


@router.post("/families/{family_id}/os-profiles/{os_profile_id}")
async def attach_os_profile(
    family_id: int,
    os_profile_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    family = ProductFamilyDAO.get_by_id(db, family_id)
    os_profile = OSProfileDAO.get_by_id(db, os_profile_id)
    if not family or not os_profile:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Family or OS profile not found")
    ProductFamilyOSProfileDAO.attach(db, family_id, os_profile_id)
    return {"status": "ok"}


@router.delete("/families/{family_id}/os-profiles/{os_profile_id}", status_code=status.HTTP_204_NO_CONTENT)
async def detach_os_profile(
    family_id: int,
    os_profile_id: int,
    auth: dict = Depends(require_admin),
    db: Session = Depends(get_db),
):
    ProductFamilyOSProfileDAO.detach(db, family_id, os_profile_id)
    return None
