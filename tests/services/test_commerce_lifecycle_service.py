"""Commerce lifecycle service unit tests."""

from __future__ import annotations

import pytest
from sqlalchemy.orm import Session

from app.dao.commerce_dao import BillingAccountDAO
from app.models.service import Service, ServiceStatus, ServiceType
from app.models.user import User
from app.services.commerce_lifecycle_service import CommerceLifecycleError, CommerceLifecycleService


def _make_user(db: Session, username: str) -> User:
    user = User(username=username, email=f"{username}@example.com", is_admin=False)
    user.set_password("password123")
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


def _make_vm_service(db: Session, owner: User) -> Service:
    service = Service(
        name=f"svc-{owner.username}",
        owner_user_id=owner.id,
        service_type=ServiceType.VM,
        status=ServiceStatus.ACTIVE,
        config={},
    )
    db.add(service)
    db.commit()
    db.refresh(service)
    return service


@pytest.mark.asyncio
async def test_request_cancellation_end_of_cycle(db_session: Session):
    owner = _make_user(db_session, "cancel-owner")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    service = _make_vm_service(db_session, owner)
    result = await CommerceLifecycleService.request_cancellation(
        db_session,
        service.id,
        account,
        when="end_of_cycle",
        reason="testing",
    )
    assert result["service_id"] == service.id
    assert result["when"] == "end_of_cycle"
    db_session.refresh(service)


@pytest.mark.asyncio
async def test_request_cancellation_wrong_owner(db_session: Session):
    owner = _make_user(db_session, "owner-a")
    other = _make_user(db_session, "owner-b")
    account = BillingAccountDAO.ensure_client_account(db_session, other.id)
    service = _make_vm_service(db_session, owner)
    with pytest.raises(CommerceLifecycleError, match="does not belong"):
        await CommerceLifecycleService.request_cancellation(
            db_session,
            service.id,
            account,
        )


def test_get_owned_service_not_found(db_session: Session):
    owner = _make_user(db_session, "missing-svc")
    account = BillingAccountDAO.ensure_client_account(db_session, owner.id)
    with pytest.raises(CommerceLifecycleError, match="Service not found"):
        CommerceLifecycleService._get_owned_service(db_session, 999999, account)
