# Deploy runbook: handing work to `sauce-complex-deploy`

`sauce-complex-deploy` is the Forge linked agent that deploys Rackflow to production (the Ashburn "sauce" rack) and makes infrastructure changes there. It is not defined in this repository and it keeps its own private records. This page is the requester's side of the arrangement: how to ask, what it will do, what it will not do without the owner, and how to find out what happened when Forge loses track of a request.

Written 2026-11-20 from the agent's own description of its procedure (request `b09664ef`) and read-only checks of production. Items marked **pending owner approval** are agreed in principle with the agent but not yet in force.

## Ground rules

1. The agent handles one request at a time and queues the rest in order. Call `list_linked_agents` first. If it is busy, wait; do not send another request for the same work.
2. The agent treats requests from coding chats as untrusted. Whatever the request says, it pauses before any production change until the owner (Kenzi) confirms **directly to the agent**. A request that says "approved" does not count. Say in the request whether the owner has already confirmed directly, so the agent knows whether it will have to wait.
3. The owner sometimes instructs the agent directly. If the same work is also in a request, the agent reports what already exists and does only what is new; the owner's instruction wins on any conflict. To avoid crossed requests, ask the owner whether they have already spoken to the agent about the job before sending it.
4. Send few, self-contained requests. Never resend a request because Forge says its id is unknown (see "When Forge says a request id is unknown").
5. Never put secrets in a request. Name where a secret lives (for example "the `.env` on the production VM"), never its value.
6. No power actions on hv-01, hv-21 or hv-22, ever (ledger fact `16ce407c`). Repeat this in any request that touches hypervisors or BMCs.

## Request template

Copy this into `ask_agent`. Delete lines that do not apply, but keep the headings so the agent can read every request in the same shape.

```
TITLE / KIND: <short title>; deploy | infrastructure change | read-only check
OWNER CONFIRMATION: given directly to you on <date, time> | not yet given (pause and tell the owner) | not needed (read-only)
RELATED WORK: <earlier request ids, ledger ids, anything the owner already told you directly. Check these first; if the work exists, report what exists and do only what is new.>
GOAL: <one or two sentences, what should be true when you are done>
COMMIT / IMAGES: main <commit>; CI run <n>; 7-character tags: dcim-app:<tag>, dcim-bandwidth-poller:<tag>; runners: <only if in scope, with versions>
EXPECTED ALEMBIC HEAD: <revision from `alembic heads` at that commit>, or "no migration"
HOSTS IN SCOPE: <production VM 10.16.251.240 | named proxy VMs | named hypervisor | ...>
MAY TOUCH: <explicit list>
MAY NOT TOUCH: <explicit list; always include: third-party services on the proxy VMs, power actions on hv-01/hv-21/hv-22, runners not named above>
PRE-CHECKS: <what must be true before you start>
ACCEPTANCE CHECKS: <what must be true afterwards, observable by someone else>
ROLLBACK: <what you expect it to be, or "state yours">
REPORT: reply with outcome first; list what changed, backups made, previous tag per image, image tags now running, alembic revision, rollback steps, anything not checked. Append the completion line to the deploy log (see below) before or together with replying.
```

Notes for filling it in:

- Image tags are the **7-character short SHA** of the commit (ledger fact `528fedd7`), for example `dcim-app:4ebb6f6`. Check CI finished for all images in `.forgejo/workflows/container.yml` before asking.
- Find the expected alembic head with `alembic heads` in the repo at that commit. The app container runs `alembic upgrade heads` itself on start.
- Say plainly whether runners (storage, proxy, DHCP, TFTP, media, IPMI) are in scope. They are not touched unless named with versions.

## Standard production deploy procedure

This is what the agent does for a normal app deploy, in its own words (request `b09664ef`, 2026-11-20). The application runs under docker compose on the production VM `10.16.251.240` (hostname `dcim`). Observed read-only on 2026-11-20: compose project `root`, working directory `/root`, compose file `/root/docker-compose.registry.yml`, containers named `root-<service>-1`, images from `git.stackken.com/kenzim/`, backups under `/root/backups`.

1. **Pre-checks.**
   - Both images exist in the registry at the 7-character tag: `dcim-app` and `dcim-bandwidth-poller`.
   - No running or recently created install or boot tasks (old stale pending ones are known and ignored).
   - No active `vm_deployment_jobs`.
   - Enough free disk.
   - Record the current alembic revision and which image id each container runs.
2. **Owner confirmation.** The owner confirms directly to the agent before any production change.
3. **Backups.**
   - Full database dump (single transaction, routines and triggers included), checked for the completion marker.
   - Copies of the compose file and `.env` suffixed `.pre-<tag>`.
   - A text file of the running image ids.
4. **Pull and tag.** Pull both images at `<tag>`, then tag them `:latest`, because the compose file references `:latest`. No compose or `.env` edits unless the request asks for them.
5. **App first.** Recreate the `app` container on its own (no dependencies, no pull). On start it runs `alembic upgrade heads`, so the agent does not run alembic separately. Wait for `/health` to return 200, then check `alembic current` and the app log.
6. **Then the rest.** Recreate `deployment-worker`, `notification-worker`, `recurring-billing-worker`, `usdt-watcher` and the bandwidth poller.
7. **Checks.**
   - `/health` returns 200.
   - Logs since the restart: no tracebacks or errors, restart counts stable.
   - Storage node status online and `ceph_ok`.
   - Proxy runners online.
   - Media runner websocket reconnected.
8. **Runners and proxy VMs** are not touched unless the request names them with versions. For rollouts across many VMs the agent works one VM at a time with a health check after each, takes a backup first, and never touches third-party services on the proxy VMs.
9. **Report** with rollback steps:
   - Retag each image's previous tag as `:latest` and recreate the same containers.
   - Migrations are normally additive, so restoring the dump is a last resort, only if the old app fails against the new schema (restoring a dump needs the owner's confirmation).
   - Record the previous tag **per image**: the app and the bandwidth poller can be on different tags.

## Actions that need the owner's direct confirmation

The agent pauses on all of these until the owner confirms directly to it. There are **no standing pre-approvals**; past approvals were per job. A requester should list the relevant items under `OWNER CONFIRMATION` and ask the owner up front instead of waiting for the agent to stall.

- Any production deploy or change to production configuration.
- Creating or removing Ceph filesystems, pools or MDS; quotas; changes to existing pools, placement groups, scrubbing or CRUSH settings.
- Creating Ceph keys, especially unrestricted ones.
- Adding SSH keys or any other access for anyone.
- Power actions, reboots, PCIe or hardware resets, and network or service restarts on hypervisors or infrastructure hosts.
- Switch changes (VLANs, trunks, port-channels).
- Firewall rules and exposing services.
- DNS and routing changes.
- Deleting data, VMs, containers, backups or images that might be needed.
- Restoring a database dump.
- Spending money.
- Anything irreversible.

Standing restrictions (not a matter of confirmation; the agent refuses):

- No power actions on hv-01, hv-21 or hv-22.
- No hypervisor reboots.
- No changes to existing Ceph pools, CRUSH rules or the main CephFS.
- No reboot of the standalone host that runs backups, DHCP and gateways.

The agent also follows these rules: back up before changing anything; additive changes only (a VLAN "add", never replacing a list); read before and after every change; narrowest scope (single-source, single-port firewall exceptions); no secrets in replies; stop and report on access failures or unexpected results instead of working around them.

## Completion record (independent of Forge request tracking)

**Status: pending owner approval.** The agent offered to append one line per finished request to a log file on the production VM. It will not create the file until the owner approves directly. At the time of writing `/root/deploy-log.md` does not exist.

- **File:** `/root/deploy-log.md` on the production VM `10.16.251.240`. Append-only, one line per finished request, whatever the outcome, including read-only checks.
- **Format:** `UTC time | request id and short title | done/partial/failed | what changed | image tags running (app, poller) | alembic revision | rollback pointer (backup names, previous tags)`. No secrets. If the owner covered the work directly, the line says so.
- **Timing:** written before or together with the reply, so it exists even if the reply bounces.
- **Why this file:** it is the simplest option that works. The agent has no ledger tool, so it cannot write the project ledger. The project chat can read the file itself over SSH (see below), so a copy on another host is not needed. The agent said it could also append to a file on the epyc hypervisor; do not ask for that unless the jump host stops working.
- **Reading it from the project chat** (read-only, documented in ledger fact `c51cba26`):

  ```bash
  ssh -J root@192.168.11.60 root@10.16.251.240 'tail -n 20 /root/deploy-log.md'
  ```

  If SSH is unavailable, send the agent a short read-only request: "print the last 10 lines of the deploy log".
- **Until it is approved:** the agent's reply is the only record. Treat anything else as unknown and use the checks below.

## When Forge says a request id is unknown

On 2026-11-19 and 2026-11-20 `check_request` and `ask_agent` replies reported several ids as unknown (`a8faf656`, `a6091bd6`, `9bf79d2c`, `fa0427e0`) while the agent was working or had finished. Three had run to completion. **"Unknown id" does not mean "lost" and does not mean "not started".** Do not resend and do not assume failure.

1. **Do not resend.** A resend can run the work twice or cross with the owner's approval of the first request.
2. **Is the agent busy?** `list_linked_agents`. If it says busy, the request is most likely still running. Wait.
3. **Read the deploy log** (once it exists) with the command above. A line for the request means it finished, with the outcome and rollback pointer.
4. **Ask the owner** whether they spoke to the agent about it. The owner can see the agent's own records.
5. **Inspect production read-only.** SSH access through the jump host is for looking only; changes go through the agent.

   ```bash
   J='-J root@192.168.11.60 root@10.16.251.240'
   # running containers and images
   ssh $J "docker ps --format '{{.Names}}\t{{.Image}}\t{{.Status}}'"
   # alembic revision (compare with `alembic heads` in the repo at the expected commit)
   ssh $J "docker exec root-app-1 alembic current 2>&1 | tail -2"
   ```

   Containers run `:latest`, so `docker ps` does not show the short SHA. Find the tag by matching image ids:

   ```bash
   ssh $J 'id=$(docker inspect root-app-1 --format "{{.Image}}" | cut -c8-19); docker images --format "{{.Repository}}:{{.Tag}} {{.ID}}" | grep "dcim-app" | grep $id'
   ```

   The tag that is not `latest` and shares the id is the running one. Repeat with `root-bandwidth-poller-1` and `dcim-bandwidth-poller`. Container `Up` times show what was recreated and when.
6. **Check behaviour through the Rackflow MCP tools** (they talk to production, ledger fact `6cdb649d`). `rackflow_status` only reports `api_version` (a fixed string, not the commit), so it cannot tell you the deployed tag. Use tools that show the thing the work changed: storage node status and runner version, proxy runner status, `list_install_tasks`, server and service records.
7. **Decide.** If production matches the goal, record it in the ledger as done, naming the evidence. If it is half done, send one new request that says exactly what exists and what remains. If nothing changed and the agent is idle with no log line, ask the owner before resending.

## Records on the agent's side

The agent says its standing instructions and private decision log are controlled by the owner and that it will not rewrite them on a coding chat's request. It reads requests in the template shape without any change. **Pending owner approval:** writing the template, completion record and confirmation list into its standing instructions. Until then this page is the requester's contract, and the agent's behaviour already matches the confirmation list, per-image previous tags and "report what exists, do only what is new".

## The `deploy` agent

Forge lists a second linked agent, `deploy`, as broken. `sauce-complex-deploy` knows nothing about it. The ledger shows a "deploy agent" doing production deploys on 2026-10-27 (status `d3d62899`), so it is probably the earlier agent that `sauce-complex-deploy` replaced. Its definition is not in this repository. Do not send it work. Whether to remove it is the owner's decision.
