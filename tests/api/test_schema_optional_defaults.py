from datetime import datetime, timezone

from app.api.asset import AssetResponse
from app.api.server_interaction import BootTaskResponse
from app.schemas.billing import BillingServiceResponse


def test_asset_response_optional_description_defaults_to_none():
    model = AssetResponse(id=1, filename="logo.png", label="server_logo", created_at="2026-01-01T00:00:00Z")
    assert model.description is None


def test_boot_task_response_optional_fields_default_to_none():
    model = BootTaskResponse(
        id=1,
        server_id=5,
        boot_type="temp_os",
        status="pending",
        created_at="2026-01-01T00:00:00Z",
    )
    assert model.kernel_url is None
    assert model.initrd_url is None
    assert model.kernel_params is None
    assert model.script_url is None
    assert model.error_message is None
    assert model.started_at is None
    assert model.completed_at is None


def test_billing_service_response_optional_fields_default_to_none():
    now = datetime.now(timezone.utc)
    model = BillingServiceResponse(
        id=1,
        name="svc-1",
        status="pending",
        created_at=now,
        updated_at=now,
    )
    assert model.external_service_id is None
    assert model.description is None
    assert model.config is None
