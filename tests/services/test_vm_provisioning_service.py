"""
Tests for VMProvisioningService.plan_provisioning's spec normalization.

The product catalog UI (family/product VM config) stores sizing under
``cpu_cores`` / ``ram_mb``; legacy family defaults / product overrides use
``cpu_count`` / ``ram_mb``. The Proxmox executor
(``app/services/vm_strategy_executor.py``) applies sizing from canonical
``cores`` / ``memory_mb`` keys. ``plan_provisioning`` must always populate
those canonical keys regardless of which alias supplied the value.
"""
from app.dao.product_catalog_dao import ProductDAO, ProductFamilyDAO, VMTemplateDAO
from app.dao.vm_config_dao import FamilyVMConfigDAO, ProductVMConfigDAO
from app.services.vm_provisioning_service import VMProvisioningService


def _family_and_product(db_session, *, defaults=None, overrides=None):
    family = ProductFamilyDAO.create(
        db_session,
        name="Catalog Fam",
        description=None,
        code="catalog-fam-vm",
        service_type="vm",
        provisioning_backend="proxmox",
        defaults=defaults or {},
        constraints={},
    )
    product = ProductDAO.create(
        db_session,
        family_id=family.id,
        name="Catalog Prod",
        description=None,
        code="catalog-prod-vm",
        overrides=overrides or {},
    )
    tmpl = VMTemplateDAO.create(
        db_session,
        name="Linux stub",
        os_type="Linux - Guest agent",
        proxmox_template_name="linux-stub-tmpl",
        code="linux-stub",
    )
    ProductDAO.set_vm_templates(db_session, product, [tmpl.id])
    db_session.commit()
    return family, product, tmpl


def test_plan_provisioning_normalizes_catalog_vm_config_keys(db_session):
    family, product, tmpl = _family_and_product(db_session)
    FamilyVMConfigDAO.upsert(db_session, family, {"cpu_cores": 4, "ram_mb": 8192})
    db_session.commit()

    plan = VMProvisioningService.plan_provisioning(
        db_session,
        service_id=1,
        product_code=product.code,
        vm_template_id=tmpl.id,
    )

    specs = plan["effective_specs"]
    assert specs["cpu_cores"] == 4
    assert specs["ram_mb"] == 8192
    assert specs["cores"] == 4
    assert specs["memory_mb"] == 8192
    assert specs["full_clone"] is False


def test_plan_provisioning_full_clone_opt_in(db_session):
    family, product, tmpl = _family_and_product(db_session)
    FamilyVMConfigDAO.upsert(db_session, family, {"cpu_cores": 2, "ram_mb": 2048})
    ProductVMConfigDAO.upsert(
        db_session, product, extends_family=True, config={"full_clone": True}
    )
    db_session.commit()

    plan = VMProvisioningService.plan_provisioning(
        db_session,
        service_id=4,
        product_code=product.code,
        vm_template_id=tmpl.id,
    )

    assert plan["effective_specs"]["full_clone"] is True


def test_plan_provisioning_product_override_alias_wins(db_session):
    family, product, tmpl = _family_and_product(db_session)
    FamilyVMConfigDAO.upsert(db_session, family, {"cpu_cores": 2, "ram_mb": 2048})
    ProductVMConfigDAO.upsert(db_session, product, extends_family=True, config={"ram_mb": 4096})
    db_session.commit()

    plan = VMProvisioningService.plan_provisioning(
        db_session,
        service_id=2,
        product_code=product.code,
        vm_template_id=tmpl.id,
    )

    specs = plan["effective_specs"]
    assert specs["cpu_cores"] == 2
    assert specs["ram_mb"] == 4096
    assert specs["cores"] == 2
    assert specs["memory_mb"] == 4096


def test_plan_provisioning_legacy_defaults_overrides_alias(db_session):
    family, product, tmpl = _family_and_product(
        db_session,
        defaults={"cpu_count": 2, "ram_mb": 2048},
        overrides={"ram_mb": 4096},
    )
    db_session.commit()

    plan = VMProvisioningService.plan_provisioning(
        db_session,
        service_id=3,
        product_code=product.code,
        vm_template_id=tmpl.id,
    )

    specs = plan["effective_specs"]
    assert specs["cpu_count"] == 2 or specs.get("cpu_cores") == 2
    assert specs["ram_mb"] == 4096
    assert specs["memory_mb"] == 4096
