from cryptography.fernet import Fernet
from pydantic import SecretStr

from app.core.config import settings
from app.models.reseller import Reseller
from app.models.user import User
from app.services.invoice_service import InvoiceService


MNEMONIC = "test test test test test test test test test test test junk"
CONTRACT = "0x1111111111111111111111111111111111111111"


class DepositRpc:
    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return None

    def assert_chain_id(self, chain_id):
        assert chain_id == 11155111

    def block_number(self):
        return 1234


def _configure(monkeypatch):
    key = Fernet.generate_key()
    ciphertext = Fernet(key).encrypt(MNEMONIC.encode()).decode()
    values = {
        "usdt_rpc_url": "https://sepolia.example.invalid",
        "usdt_chain_id": 11155111,
        "usdt_contract_address": CONTRACT,
        "usdt_hd_mnemonic_ciphertext": SecretStr(ciphertext),
        "usdt_fernet_key": SecretStr(key.decode()),
        "usdt_watcher_enabled": True,
    }
    for name, value in values.items():
        monkeypatch.setattr(settings, name, value)
    monkeypatch.setattr("app.api.reseller_panel._usdt_rpc", lambda: DepositRpc())


def _login(client, db_session, suffix):
    user = User(
        username=f"usdt-api-{suffix}",
        email=f"usdt-api-{suffix}@example.com",
        is_reseller=True,
    )
    user.set_password("panel-password")
    reseller = Reseller(user=user)
    db_session.add(reseller)
    db_session.commit()
    response = client.post(
        "/api/users/login",
        json={"username": user.username, "password": "panel-password"},
    )
    assert response.status_code == 200
    return reseller, {"Authorization": f"Bearer {response.json()['token']}"}


def test_usdt_deposit_is_tenant_scoped_idempotent_and_secret_free(
    client,
    db_session,
    monkeypatch,
):
    _configure(monkeypatch)
    first, headers = _login(client, db_session, "first")
    second, _second_headers = _login(client, db_session, "second")
    invoice = InvoiceService.create_topup(
        db_session, reseller_id=first.id, amount_cents=1234
    )
    foreign_invoice = InvoiceService.create_topup(
        db_session, reseller_id=second.id, amount_cents=500
    )
    db_session.commit()

    dashboard = client.get("/api/reseller-panel/dashboard", headers=headers)
    assert dashboard.json()["usdt_enabled"] is True
    forbidden = client.post(
        f"/api/reseller-panel/invoices/{foreign_invoice.id}/usdt-deposit",
        headers=headers,
    )
    assert forbidden.status_code == 404

    first_response = client.post(
        f"/api/reseller-panel/invoices/{invoice.id}/usdt-deposit",
        headers=headers,
    )
    replay = client.post(
        f"/api/reseller-panel/invoices/{invoice.id}/usdt-deposit",
        headers=headers,
    )
    assert first_response.status_code == 201, first_response.text
    assert replay.status_code == 201, replay.text
    payload = first_response.json()
    assert replay.json()["deposit_address"] == payload["deposit_address"]
    assert payload["chain_id"] == 11155111
    assert payload["network"] == "ethereum-sepolia"
    assert payload["amount"] == "12.340000"
    assert payload["payment_uri"].endswith(
        f"address={payload['deposit_address']}&uint256=12340000"
    )
    serialized = str(payload).lower()
    assert "mnemonic" not in serialized
    assert "private" not in serialized
    assert "derivation" not in serialized
    assert MNEMONIC not in serialized

    status = client.get(
        f"/api/reseller-panel/invoices/{invoice.id}/usdt-deposit",
        headers=headers,
    )
    assert status.status_code == 200
    assert status.json()["deposit_address"] == payload["deposit_address"]
