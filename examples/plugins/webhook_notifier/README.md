# Chat webhook notifier

Posts a one-line message to a Discord or Slack channel when a ticket is opened, an invoice is paid or a service is suspended. It is an example of an event-driven plugin that makes outbound HTTP requests and keeps a secret in its settings.

## Install

Copy this folder into Rackflow's plugins directory and install it from the Plugins page. It asks for these permissions:

| Permission | Why |
|---|---|
| `service.read` | to receive `service.suspended` and read the service name |
| `billing.read` | to receive `invoice.paid` |
| `support.read` | to receive `ticket.created` |
| `http.outbound` | to post to the webhook |

Then open the plugin's settings and paste the webhook URL (a secret: stored encrypted, shown only as a mask), choose Discord or Slack, and switch off any message you do not want.

## How it behaves

- Messages go out from the plugin worker, not the request that caused the event, so a slow chat service never slows Rackflow.
- A refused request (400 to 499 other than 408, 425 and 429) is logged as an error and not retried. A server error or a rate limit makes the delivery fail, and Rackflow retries it with a back-off for up to ten attempts.
- Events can be delivered twice, so the plugin remembers the last 200 event ids and posts each event once.
- Mentions in ticket subjects are neutralised (`@everyone` does not ping anyone), and Slack control characters are escaped.
- Without a webhook URL it posts nothing and writes a warning to the plugin log.

## Tests

`pytest examples/plugins/webhook_notifier/tests` from a Rackflow checkout. The tests use the [plugin test kit](../../../docs/plugins/testing.md): they install the plugin, fire events and check the requests the fake HTTP layer saw.
