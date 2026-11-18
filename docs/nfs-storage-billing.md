# Selling NFS storage

NFS storage (`nfs_storage`) is a product of its own. It is billed separately from servers and is
kept while the client has no servers at all, so someone who rents servers by the day or week keeps
their data with us in between. Clients reach it over NFS from their private network and over SFTP
from anywhere (see `docs/nfs-storage-sftp.md`).

Prices live only in WHMCS (or the Rackflow store), never in Rackflow code.

## How sizes are sold

**Decision: one WHMCS product per location group, with a "Storage Size" configurable option.** One
product with size choices keeps upgrades and downgrades simple: the client changes the option, WHMCS
runs ChangePackage, and Rackflow resizes the storage. Product tiers (one WHMCS product per size)
also work, because the module falls back to the Rackflow catalogue product's `size_gb`, but then
every size change is a product upgrade.

Size order of precedence when the storage is created or changed:

1. the configurable option `Storage Size` (also read as `size_gb` or `Storage Size (GB)`); the
   number before `|` is the size in GB, for example `500|500 GB`;
2. the Rackflow catalogue product's `size_gb` (the WHMCS Product Code points at it).

A resize never goes below the total quota of the client's shares; WHMCS shows the error if it would.

## How the location is chosen

Add a configurable option `Storage Location` (also read as `location_id` or `Location`). The number
before `|` is the Rackflow location ID, for example `1|London`. Rackflow refuses an order for a
location without an enabled storage node. Without the option the client chooses the location when
linking a network: if only one location offers storage it is used, otherwise they pick one.

Storage ordered for a location can only be linked to a private network in that location.

## Rackflow setup

1. **Storage node:** in Admin, Storage, make a server a storage node and install the runner
   (`storage_runner/README.md`). For SFTP, set the node's public host and port and turn SFTP on;
   open that TCP port to the node from the internet.
2. **Catalogue:** under Products, the storage family (service type `nfs_storage`) holds one product
   per default size, for example code `nfs-500` with `size_gb` 500. The code is what WHMCS sends.
3. **Locations:** note the Rackflow location IDs of the locations with storage nodes, for the
   `Storage Location` option values.
4. **Private network pools:** each storage location needs a private network pool, because storage
   always sits on a client network (one is created for the client if they have none).

## WHMCS setup

1. **Product:** create a product (type Other) with module RackFlow. In Module Settings choose
   **NFS storage** as the service type. The panel then shows only what storage uses:
   - **Product Code:** the Rackflow catalogue product (its `size_gb` is the default size);
   - **Client portal sign-in:** whether the client area shows the "Open portal" button.

   Server, VM template, OS and SSH key settings are hidden for storage, and saving the product does
   not attach the OS option group or the SSH keys custom field to it.
2. **Configurable options:** create a group with:
   - `Storage Size` (dropdown): values such as `100|100 GB`, `500|500 GB`, `1000|1 TB`, each with its
     price per billing cycle;
   - `Storage Location` (dropdown, optional): values such as `1|London`.

   Assign the group to the product. Allow upgrades and downgrades of configurable options so clients
   can change size.
3. **Lifecycle:** Create provisions the storage; Suspend makes the shares unreachable over NFS and
   SFTP but keeps the data; Unsuspend restores access; Terminate removes access and destroys the data
   after the grace period (`STORAGE_DESTROY_GRACE_SECONDS`, default 7 days). Storage has no power
   actions.
4. **Client area:** the product page shows the size and usage, location, linked network, shares
   with their NFS mount and fstab lines, the SFTP host, port, host key fingerprint and logins, and
   the "Open portal" button when sign-in is allowed. Shares, the network link and SFTP logins are
   managed in the Rackflow portal (or the client MCP `storage` tool).

## Rackflow store

The Rackflow store can sell storage too. Create a store product linked to the storage catalogue
product (service type `nfs_storage`), then add select options whose values carry the provision keys
`size_gb` (value: size in GB) and `location_id` (value: Rackflow location ID). Changing size later
is done by staff (Admin, Storage, Resize) or through WHMCS.

## Retention without servers

Terminating, suspending or letting every server of a client expire never touches their storage, its
private network or the storage node's attachment to that network. Private networks belong to the
client and stay until the client deletes them; a network with storage on it cannot be deleted. This
is covered by `tests/api/test_storage_product.py`.
