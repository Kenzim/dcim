# Service notes

A label and a note on every VM and bare metal service. The client edits them in the client portal or WHMCS, staff edit them in the admin interface, and a daily schedule flags a note nobody has changed for a while and tells the client. It is an example of the declarative interface from end to end: one panel, three actions with a form and a confirmation, per-service values that prefill the form from state, a schedule, a client notice and a staff MCP tool. There is no plugin JavaScript, HTML or PHP: every surface draws the same render document.

## Install

Copy this folder into Rackflow's plugins directory and install it from the Plugins page. Permissions:

| Permission | Why |
|---|---|
| `service.read` | the plugin applies to services |
| `ui.client` | clients see the panel and press its buttons |
| `notify.client` | the review reminder to the client |
| `mcp.staff` | the staff MCP tool `get_note` |

It is active for every VM and bare metal service (`enablement` is `all`). The two settings are the number of days after which a note is flagged, and whether to tell the client.

## Where to see it

- **Admin interface:** service page, Plugins section. Staff see who last changed the note and the "Mark as reviewed" button.
- **Client portal and WHMCS:** the same panel without the staff-only parts; clients can save and clear.
- **Staff MCP:** `plugins(action="call", plugin_id="service_notes", tool="get_note", params={"service_id": 1})`, and `plugins(action="panel", service_id=1)` for the document.

## Tests

`pytest examples/plugins/service_notes/tests` from a Rackflow checkout. They use the [plugin test kit](../../../docs/plugins/testing.md) to run the actions as a client and as an admin, check the panels each audience gets, and run the schedule with a moved clock.
