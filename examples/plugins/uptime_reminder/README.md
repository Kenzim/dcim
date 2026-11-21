# Uptime reminder (example plugin)

A small, harmless plugin that shows most of the Rackflow plugin API 1.0 (see `docs/plugin-system.md`):

- **Events**: starts a reminder period when a service is created or unsuspended, pauses it when the service is suspended.
- **Schedule**: an hourly per-service check that marks the service "Please confirm" once the period has passed, sends the client a notice (the client must opt in to "Notices from plugins") and optionally posts to a webhook.
- **Settings**: reminder period, client notices on or off, an optional webhook URL and a secret webhook token (stored encrypted, sent as a Bearer token).
- **Per-service state**: status, last confirmation, next reminder, the client's note and the number of reminders sent.
- **Status panel**: shown on the service page in the admin UI, the client portal and WHMCS.
- **Client action**: "Confirm I still need this", with confirmation text and an optional note.
- **Staff action and MCP tool**: "Ask the client now", and the `status` tool through the staff MCP `plugins` tool.

To try it, copy or link this directory into the plugins directory (`PLUGINS_DIR`, default `plugins/`), then install it from **Admin, Plugins**.
