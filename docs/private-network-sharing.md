# Private network sharing

A client (the **owner**) can invite other clients onto one of their private networks. An accepted invitee becomes a **member**: they can attach their own virtual machines and dedicated servers to the network, and so reach any NFS storage share that is open to the whole network. Everything that manages the network stays with the owner. This document is the design; `docs/private-networks.md` describes the network feature itself.

## Roles

| Capability | Owner | Member | Anyone else |
|---|---|---|---|
| See the network (name, location, address range, storage address) | yes, plus VLAN ID | yes, without the VLAN ID | no |
| Attach and detach their own services | yes | yes (own services only) | no |
| See shares open to the whole network, with mount lines | yes | yes (those shares only) | no |
| Create and manage shares, SFTP logins, link or resize storage | yes | no | no |
| Rename (not offered today) or delete the network | yes | no | no |
| Invite, cancel an invitation, remove a member | yes (staff can remove) | no | no |
| See other members and their servers | yes | no | no |
| Leave | n/a | yes | no |

A member never sees the owner's other services, billing or personal details. The only thing about the owner they are shown is the owner's account username, as the display name ("shared by"). The owner sees each member's username and the addresses (and kind, VM or dedicated server) of the member's attached servers, but not the member's service names, MAC addresses or any other service.

Proxy access networks (`kind = proxy_access`) can never be shared: they have no members, invitations are refused, and the member routes never match them. Storage stays the owner's: linking, resizing and share management use the existing owner-only storage API, which is unchanged.

## Reseller boundary

Users have an optional `reseller_id`. Resellers' own accounts are never clients (`is_reseller`), and staff (`is_admin`) are never clients.

**Decision:** an invitation only works between two client accounts with the same `reseller_id`. Direct clients (`reseller_id` empty) can share with other direct clients, and clients of one reseller can share only with other clients of that reseller. A client of reseller A can never be invited by, or invite, a client of reseller B or a direct client. The default is the safe one: sharing never reveals one reseller's customers to another reseller's customers, and never lets a reseller's customer reach the operator's own customers by accident. The check runs when inviting and again when accepting. The repair loop also removes a member whose reseller no longer matches the owner's.

## Inviting without revealing who has an account

The owner types an email address or a username. To stop the form being used to find out which addresses are registered, the answer never depends on whether an account matches: an invitation is always recorded and shown as pending. Only an invitation that matches an eligible client (a client account, same reseller, not the owner, not already a member) is shown to anyone, and it is the only kind that can be accepted. A match uses the account email or the username, case-insensitively. A non-matching invitation expires like any other. Both kinds count against the network's limits, so the form cannot be used to send an unlimited number of invitations.

Notices (email or webhook) to the invitee are fixed sentences. They never contain the network name or the inviter's name, because both are free text chosen by another client; the portal shows them to the invitee after sign-in.

## Data model

New tables (one migration, chained on the single head):

- `private_network_members`: `network_id` (cascade), `user_id` (cascade), `invited_by_user_id`, `joined_at`; unique on (`network_id`, `user_id`).
- `private_network_invitations`: `network_id` (cascade), `inviter_user_id`, `invitee_user_id` (empty when nothing matched), `invitee_label` (what the owner typed, shown only to the owner), `status` (`pending`, `accepted`, `declined`, `cancelled`, `expired`), `active_key` (unique, set only while pending, so one pending invitation per network and invitee), `created_at`, `expires_at`, `responded_at`.

New columns: `private_network_pools.max_members_per_network` (default 5; 0 turns sharing off for the location), `private_networks.owner_gone_since`.

New settings: `PRIVATE_NETWORK_INVITATION_DAYS` (14) and `PRIVATE_NETWORK_OWNER_GONE_GRACE_DAYS` (30).

An attachment has no new column. Whose attachment it is follows from `service.owner_user_id`, and the rule "the owner of an attached service must be the network owner or a member" is enforced when attaching and again by the repair loop.

## Limits

- **Members per network:** the pool's `max_members_per_network` (default 5). Members plus pending invitations count, so a network cannot be over-invited. Lowering the limit never removes existing members.
- **Addresses:** a member's attachments take addresses from the network's range exactly like the owner's, so they use up the same range (and the reserved storage address is never given out).
- **Networks per client:** `max_networks_per_client` counts only networks the client owns. Joining a network does not use it.
- **Networks per service:** unchanged; a service can join at most `max_networks_per_service` networks, owned or shared.

## Lifecycle

| Event | What happens |
|---|---|
| Owner removes a member, or staff do | The member row is deleted first, so the member loses visibility and every member route fails at once. Then all attachments of the member's services are detached (including the switch VLAN removal) and the member's addresses are taken off the network's restricted shares. If a switch cannot be reached the attachment stays `detaching` and the repair loop retries; the member cannot see or change it meanwhile. The member is notified. |
| Member leaves | Same as removal, started by the member. The owner is notified. |
| Owner cancels a pending invitation | The invitation becomes `cancelled` and disappears for the invitee. |
| Invitee accepts or declines | Accepting re-checks eligibility, expiry and the member limit. The owner is notified. |
| Invitation not answered | It expires after `PRIVATE_NETWORK_INVITATION_DAYS` and cannot be accepted. Answered invitations are deleted after 90 days. |
| Owner deletes the network | Allowed once the owner's own services are detached and no storage is linked (both as before). The members are removed with it: their attachments are detached, pending invitations are cancelled, and the members are notified. The VLAN is then deleted as usual. |
| Storage is linked to the network | The storage belongs to the owner. Members can see and mount open shares through the network but cannot manage them. A network with storage cannot be deleted (as before). |
| Owner's services are suspended | Nothing changes for members. Suspension is about the owner's own services; the network is free and unmetered. |
| Owner's services are terminated | Their attachments are released as before. Members stay. |
| Owner has no live service (none pending, active or suspended) for `PRIVATE_NETWORK_OWNER_GONE_GRACE_DAYS` (30) | Only for networks that have members. The repair loop records `owner_gone_since` the first time it sees this and clears it if the owner gets a live service again. When the grace has passed it removes every member (their servers are detached and they are notified). The network stays with the owner as an unshared network, as any dormant client network does today. Members therefore cannot keep a network or VLAN alive after the owner has left. |
| Owner's account is deleted | The network loses its owner (`owner_user_id` becomes empty). The repair loop then winds up any such network that has members: removes all members, detaches everything and, when no storage is linked, deletes the network and its VLAN. |
| Member's attached service is terminated | The attachment is released as before; the membership stays so the client can attach another service. |
| Member's account is deleted | The membership is deleted with the account and the repair loop detaches any attachment whose service is no longer owned by the owner or a member. |
| A service changes owner | The same repair rule detaches it from networks its new owner does not belong to. Storage services are exempt. |
| Member's service is suspended | Nothing changes, as for the owner's own suspended services. |

## Surfaces

- **Client API** (`/api/client/private-networks`): owner routes keep their paths and stay owner-only (`GET /{id}` now also lists members and invitations). New: `GET /invitations` (my pending invitations), `POST /invitations/{id}/accept|decline`, `POST /{id}/invitations` (invite), `DELETE /{id}/invitations/{invitation_id}`, `DELETE /{id}/members/{user_id}`. Member routes are all under `/shared`: `GET /shared`, `GET /shared/{id}`, `POST /shared/{id}/attachments`, `DELETE /shared/{id}/attachments/{attachment_id}`, `GET /shared/{id}/storage`, `POST /shared/{id}/leave`. Keeping the two sets of paths apart means a member can never reach an owner route and an owner never reaches a member route by mistake. A network that is not yours, or not shared with you, is always "not found".
- **Staff API** (`/api/admin/private-networks`): the network now lists members and invitations; `DELETE /{id}/members/{user_id}` removes a member.
- **Portal:** the network page gets a Members and invitations panel for owners; `/client/networks` gets a "Shared with me" section with the invitations to answer, the shared networks, attach and detach, and the storage address with its mount lines.
- **Audit log:** `private_network.invite`, `.invitation_cancel`, `.invitation_accept`, `.invitation_decline`, `.member_remove`, `.leave`, `.attach` and `.detach` for shared networks, `.share_end` (owner gone or network deleted). The staff-visible events name the network and the users by id.
- **Client notifications:** one new opt-in category, `network_sharing`: invitation received, accepted, declined, member left, removed, network ended. Fixed sentences only.
- **Client MCP:** `services` gets `view="networks"` (owned networks with members and invitations, shared networks with their open shares, and my invitations). `service_action` gets `action="network"` with the sub-action in `name` and its arguments in `params`: `attach`, `detach`, `invite`, `invitation_cancel`, `member_remove`, `leave`, `invitation_accept`, `invitation_decline`. `service_id` becomes optional for that action only. No new tool.
- **Staff MCP:** `lookup` gets `kind="private_networks"` (optionally by `location_id`) and `switch_action` gets `action="network_member_remove"` (`network_id`, `member_user_id`). No new tool.
- **WHMCS:** private networks are not shown in the WHMCS client area (only storage text mentions them), so there is no WHMCS change.

## Tests

Owner-only actions are tried as a member and as an unrelated client through the client API and the client MCP; member actions are tried as an unrelated client; a removed member is checked to have no attachments and no access straight away. See `tests/api/test_private_network_sharing_api.py`, `tests/services/test_private_network_sharing.py` and `tests/mcp/test_private_network_sharing_mcp.py`.
