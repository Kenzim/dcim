# RackFlow Reseller WHMCS module

This is a separate provisioning module for RackFlow resellers. It authenticates
with a reseller API key and uses only the tenant-scoped reseller API. It does
not load or depend on the standard `rackflow` WHMCS module.

## Configure

1. Copy this directory to `modules/servers/rackflow_reseller/`.
2. In WHMCS, create a server with module `RackFlow Reseller`.
3. Set Hostname/IP, Port, and SSL to the RackFlow API. Put the one-time reseller
   API key in **Access Hash** (preferred) or **Password**.
4. Assign that server (or its WHMCS server group) to the product.
5. Select Service Type, Product Code, and the applicable cluster/server group.
   Optionally enable Customer OS Selection and client portal sign-in.
6. Open Module Settings on a RackFlow Reseller product and **Save Changes** once
   so WHMCS registers `hooks.php` (adds `rackflow_reseller` to `ModuleHooks`).

WHMCS product prices are retail prices and are never synchronized by this
module. RackFlow separately charges the reseller's effective wholesale setup
plus first-month price during deployment, then handles recurring wholesale
charges.

Provisioning uses `Idempotency-Key: whmcs-service-{serviceid}`. A payment-
required response does not create a service. Fund the returned RackFlow invoice
and retry the same WHMCS module command; do not change the idempotency key.

TLS certificate and hostname verification are mandatory. Install the issuing CA
on the WHMCS host instead of disabling verification.
