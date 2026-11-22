# VIES VAT number validator

Checks EU VAT numbers with the European Commission's VIES service. Rackflow uses the answer to decide whether reverse charge applies. It is a first-party plugin: it ships in `plugin_packages/` and appears on the admin plugin page as bundled. Nothing installs or enables it automatically; an administrator installs it, grants `tax.validate_id` and `http.outbound`, and enables it.

Plugin id `vies_validator`, provider `tax_id_validator:vies`, ID type `eu_vat`, countries the 27 member states with `EL` for Greece. Northern Ireland (`XI`) is not covered.

## Settings

| Setting | Meaning |
|---|---|
| Your VAT country, Your VAT number | Your own number. When both are set, VIES returns a consultation number (`requestIdentifier`) that Rackflow stores as the check's reference. This is the evidence a tax authority accepts. Values on the check request, if billing supplies them, take priority. |
| VIES endpoint | Default is the Commission's REST address. Set it to `https://ec.europa.eu/taxation_customs/vies/rest-api/check-vat-test-service` to try the plugin with the test numbers below. |

## How answers are mapped

| VIES answer | Result |
|---|---|
| `valid: true` | `valid`, with the registered name and address (the placeholder `---` becomes empty) and the consultation number |
| `valid: false` | `invalid` |
| `INVALID_INPUT` | `invalid`, `error_code` `INVALID_INPUT` |
| `MS_UNAVAILABLE`, `SERVICE_UNAVAILABLE`, `TIMEOUT`, `MS_MAX_CONCURRENT_REQ`, `GLOBAL_MAX_CONCURRENT_REQ` and their `_TIME` variants | `unavailable` with the code |
| `INVALID_REQUESTER_INFO`, `VAT_BLOCKED`, `IP_BLOCKED` | `unavailable` with the code; they say nothing about the customer's number. `INVALID_REQUESTER_INFO` also writes an error line to the plugin log asking the operator to fix the requester settings. |
| Any other code, an unreadable body, a missing `valid` flag, an HTTP error without a VIES error code, a network failure | `unavailable` with the code, `HTTP_<status>`, `BAD_RESPONSE` or `NETWORK_ERROR` |

An unavailable answer is never turned into `invalid`. After `MS_UNAVAILABLE` for a member state, the plugin answers `unavailable` (`MS_UNAVAILABLE`) for that state for five minutes without calling VIES. The rate limit (60 calls per 60 seconds) and the circuit breaker belong to the host; the host reports a refused call as `rate_limited` or `circuit_open`, without calling the plugin.

Results are cached by Rackflow, not by the plugin. The plugin keeps only the back-off times.

## Test service numbers

Recorded on 2026-11-22 against the Commission's test service, with country `DE` (fixture `tests/fixtures/test_service.json`):

| Number | Result |
|---|---|
| 100 | valid, name "John Doe" |
| 200 | invalid |
| 201 | `INVALID_INPUT` |
| 202 | `INVALID_REQUESTER_INFO` |
| 300, 999 | `SERVICE_UNAVAILABLE` |
| 301 | `MS_UNAVAILABLE` |
| 302 | `TIMEOUT` |
| 400 | `VAT_BLOCKED` |
| 401 | `IP_BLOCKED` |
| 500, 501 | `GLOBAL_MAX_CONCURRENT_REQ`, `GLOBAL_MAX_CONCURRENT_REQ_TIME` |
| 600, 601 | `MS_MAX_CONCURRENT_REQ`, `MS_MAX_CONCURRENT_REQ_TIME` |

## Differences from the Commission's documentation

- The published contract (`swagger_publicVAT.yaml`) lists the response shapes but no enumeration of error codes. The codes in the table above come from the recorded test service answers, so any code not exercised by the test service is handled as an unknown code (`unavailable`).
- The contract documents HTTP 400 and 500 with an `errorWrappers` body. The test service answers every case, including errors, with HTTP 200 and `actionSucceed: false`. The plugin reads `errorWrappers` whatever the HTTP status is, so both behaviours map to the same results.
- The test service returns an empty `requestIdentifier` even for the valid number, because it is not given requester details. The consultation number shape in `tests/fixtures/constructed.json` was written by hand from the contract; it was not recorded.
- Number formats in `check_format` are the per-country patterns VIES publishes. Rackflow normalises numbers (upper case, no spaces or country prefix) before they reach the plugin.

## Tests

`pytest plugin_packages/vies_validator/tests`. No test calls the network; the recorded answers are replayed through the plugin test kit. The same tests also run from `tests/plugin_host/test_vies_validator.py`, because the default test paths do not include `plugin_packages/`.
