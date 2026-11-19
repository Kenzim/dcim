"""Private network sharing service: invitations, eligibility, notices and the repair loop."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from app.dao.private_network_dao import PrivateNetworkDAO
from app.models.alerting import ClientNotificationPref, NotificationDelivery
from app.models.private_network import InvitationStatus, PrivateNetworkInvitation, PrivateNetworkMember
from app.models.reseller import Reseller
from app.services import private_network_sharing as sharing
from app.services.private_network_service import PrivateNetworkError, PrivateNetworkService
from tests.api.test_private_network_sharing_api import _user, _vm, proxmox, world  # noqa: F401  (fixtures)


async def _network(db, owner, world, name="backend"):
    return await PrivateNetworkService.create_network(
        db, owner_user_id=owner.id, location_id=world.location.id, name=name
    )


async def _join(db, network, owner, member):
    await sharing.invite(db, network.id, owner.id, member.email)
    invitation = PrivateNetworkDAO.pending_invitations_for_user(db, member.id)[0]
    await sharing.respond(db, invitation.id, member.id, True)


def _opt_in(db, user, category="network_sharing"):
    db.add(ClientNotificationPref(user_id=user.id, email_enabled=True, categories={category: True}))
    db.commit()


@pytest.mark.asyncio
async def test_inviting_twice_keeps_one_pending_invitation(db_session, world):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network = await _network(db_session, alice, world)
    first = await sharing.invite(db_session, network.id, alice.id, "Bob@Example.com")
    by_name = await sharing.invite(db_session, network.id, alice.id, "bob")
    assert by_name.id == first.id and PrivateNetworkDAO.count_pending_invitations(db_session, network.id) == 1
    # An address that matches nobody is stored too, once per address, and counts against the limit.
    ghost = await sharing.invite(db_session, network.id, alice.id, "ghost@example.com")
    assert (await sharing.invite(db_session, network.id, alice.id, "GHOST@example.com")).id == ghost.id
    assert PrivateNetworkDAO.count_pending_invitations(db_session, network.id) == 2
    assert PrivateNetworkDAO.pending_invitations_for_user(db_session, bob.id)[0].id == first.id


@pytest.mark.asyncio
async def test_sharing_can_be_turned_off_for_a_location(db_session, world):
    alice = _user(db_session, "alice")
    network = await _network(db_session, alice, world)
    world.pool.max_members_per_network = 0
    db_session.commit()
    with pytest.raises(PrivateNetworkError) as excinfo:
        await sharing.invite(db_session, network.id, alice.id, "bob")
    assert excinfo.value.status_code == 409


@pytest.mark.asyncio
async def test_lowering_the_limit_keeps_existing_members(db_session, world):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network = await _network(db_session, alice, world)
    await _join(db_session, network, alice, bob)
    world.pool.max_members_per_network = 1
    db_session.commit()
    assert PrivateNetworkDAO.count_members(db_session, network.id) == 1
    with pytest.raises(PrivateNetworkError):
        await sharing.invite(db_session, network.id, alice.id, "carol")


@pytest.mark.asyncio
async def test_notices_are_fixed_sentences_without_user_text(db_session, world):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    _opt_in(db_session, bob)
    _opt_in(db_session, alice)
    network = await _network(db_session, alice, world, name="Secret <b>name</b>")
    await sharing.invite(db_session, network.id, alice.id, "bob")
    invitation = PrivateNetworkDAO.pending_invitations_for_user(db_session, bob.id)[0]
    await sharing.respond(db_session, invitation.id, bob.id, True)
    await sharing.remove_member(db_session, network.id, bob.id, by="owner", actor_user_id=alice.id)

    sent = db_session.query(NotificationDelivery).filter(NotificationDelivery.event == "network_sharing").all()
    assert {row.user_id for row in sent} == {alice.id, bob.id}
    text = " ".join(str(row.payload) for row in sent)
    assert "Secret" not in text and "alice" not in text and "bob" not in text
    assert sharing.MESSAGES["invited"] in text and sharing.MESSAGES["removed"] in text


@pytest.mark.asyncio
async def test_repair_removes_a_member_who_left_the_owners_reseller_scope(db_session, world):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network = await _network(db_session, alice, world)
    bob_vm = _vm(db_session, bob, world.location, "bob-vm")
    await _join(db_session, network, alice, bob)
    await PrivateNetworkService.attach(db_session, network.id, bob_vm.id, acting_user_id=bob.id)
    assert (bob_vm.vm.proxmox_vmid, 2000) in world.proxmox.nics

    reseller = Reseller(user_id=_user(db_session, "reseller-owner").id)
    db_session.add(reseller)
    db_session.commit()
    bob.reseller_id = reseller.id
    db_session.commit()

    counts = await sharing.reconcile_sharing(db_session)
    assert counts["removed"] == 1
    db_session.expire_all()
    assert PrivateNetworkDAO.count_members(db_session, network.id) == 0
    assert (bob_vm.vm.proxmox_vmid, 2000) not in world.proxmox.nics


@pytest.mark.asyncio
async def test_repair_expires_and_prunes_invitations(db_session, world):
    alice = _user(db_session, "alice")
    network = await _network(db_session, alice, world)
    invitation = await sharing.invite(db_session, network.id, alice.id, "bob")
    invitation.expires_at = datetime.now(timezone.utc) - timedelta(minutes=1)
    db_session.commit()
    assert (await sharing.reconcile_sharing(db_session))["expired"] == 1
    db_session.expire_all()
    row = db_session.get(PrivateNetworkInvitation, invitation.id)
    assert row.status == InvitationStatus.EXPIRED and row.active_key is None

    row.responded_at = datetime.now(timezone.utc) - sharing.ANSWERED_KEEP - timedelta(days=1)
    db_session.commit()
    await sharing.reconcile_sharing(db_session)
    db_session.expire_all()
    assert db_session.get(PrivateNetworkInvitation, invitation.id) is None


@pytest.mark.asyncio
async def test_an_owner_who_gets_a_service_back_keeps_the_members(db_session, world):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network = await _network(db_session, alice, world)
    await _join(db_session, network, alice, bob)

    await sharing.reconcile_sharing(db_session)
    db_session.expire_all()
    assert PrivateNetworkDAO.get_network(db_session, network.id).owner_gone_since is not None

    _vm(db_session, alice, world.location, "alice-vm")
    await sharing.reconcile_sharing(db_session)
    db_session.expire_all()
    refreshed = PrivateNetworkDAO.get_network(db_session, network.id)
    assert refreshed.owner_gone_since is None and PrivateNetworkDAO.count_members(db_session, network.id) == 1


@pytest.mark.asyncio
async def test_member_row_is_unique_per_network_and_user(db_session, world):
    alice, bob = _user(db_session, "alice"), _user(db_session, "bob")
    network = await _network(db_session, alice, world)
    await _join(db_session, network, alice, bob)
    assert (
        db_session.query(PrivateNetworkMember)
        .filter(PrivateNetworkMember.network_id == network.id, PrivateNetworkMember.user_id == bob.id)
        .count()
        == 1
    )
    # Bob is already a member, so a second invitation to him is stored but never shown or accepted.
    await sharing.invite(db_session, network.id, alice.id, "bob")
    assert PrivateNetworkDAO.pending_invitations_for_user(db_session, bob.id) == []


@pytest.mark.asyncio
async def test_email_style_usernames_are_masked_for_other_clients_but_not_staff(db_session, world):
    alice = _user(db_session, "alice.smith@example.com")
    bob = _user(db_session, "bob")
    network = await _network(db_session, alice, world)
    await sharing.invite(db_session, network.id, alice.id, "bob")
    shown = sharing.pending_invitations_out(db_session, bob.id)[0]
    assert shown["owner"] == "al\u2026" and "example.com" not in str(shown)

    await sharing.respond(db_session, PrivateNetworkDAO.pending_invitations_for_user(db_session, bob.id)[0].id, bob.id, True)
    carol = _user(db_session, "carol@example.com")
    db_session.add(PrivateNetworkMember(network_id=network.id, user_id=carol.id))
    db_session.commit()
    db_session.expire_all()
    network = PrivateNetworkDAO.get_network(db_session, network.id)
    names = {m["user_id"]: m["username"] for m in sharing.owner_extras(db_session, network)["members"]}
    assert names[carol.id] == "ca\u2026" and names[bob.id] == "bob"
    staff_names = {m["user_id"]: m["username"] for m in sharing.owner_extras(db_session, network, staff=True)["members"]}
    assert staff_names[carol.id] == "carol@example.com"
