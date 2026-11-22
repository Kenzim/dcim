# HMRC monthly exchange rates

Reads HMRC's published monthly exchange rates from the UK Trade Tariff service. Rackflow stores one month's rates once and uses them to show the VAT of a UK VAT invoice issued in another currency in sterling. It is a first-party plugin: it ships in `plugin_packages/` and appears on the admin plugin page as bundled. Nothing installs or enables it automatically; an administrator installs it, grants `currency.rates` and `http.outbound`, and enables it. The billing job `tax_exchange_rates_fetch` then calls it.

Plugin id `hmrc_exchange_rates`, provider `currency_rates:hmrc_monthly`, source `hmrc_monthly`. Rates are currency units per GBP 1, as HMRC publishes them.

## Settings

| Setting | Meaning |
|---|---|
| Trade Tariff address | Default `https://www.trade-tariff.service.gov.uk`. The plugin reads `/api/v2/exchange_rates/files/monthly_xml_<year>-<month>.xml` from it, with the month not zero padded (`monthly_xml_2026-9.xml`). |

## How answers are mapped

| HMRC answer | Result |
|---|---|
| HTTP 200 with the month's file | One `CurrencyRate` per currency code, with `rateNew` as given (HMRC prints up to four decimals) |
| A currency listed for several countries with the same rate | One entry |
| A currency listed with different rates | Left out, with a warning in the plugin log |
| An entry whose code is not three letters, whose rate is not a plain decimal above zero, or that is GBP | Skipped |
| HTTP 404 | Empty `rates`: the month is not published yet. Billing asks again on its next run. |
| Any other HTTP status, a network failure, XML that does not parse (including a file cut at the SDK's 64 KB limit), a DOCTYPE or entity declaration, another root element, or a `Period` attribute that is not the requested month | An exception, which the host reports as a provider failure (`ProviderUnavailable`, reason `error`) and counts for the circuit breaker. Empty `rates` always means "not published", never "could not ask". |
| A base other than GBP | Empty `rates`, without asking HMRC |

The rate limit (30 calls per hour) and the circuit breaker belong to the host. The plugin keeps no state.

## Recorded files

`tests/fixtures/` holds HMRC's files for October, November and December 2026 and the body of the 404 for January 2027, all fetched on 2026-11-22. Each file lists 141 currencies once. November gives USD 1.3396 and EUR 1.1930; December gives USD 1.3158.

## Tests

`pytest plugin_packages/hmrc_exchange_rates/tests`. No test calls the network; the recorded files are replayed through the plugin test kit. The same tests also run from `tests/plugin_host/test_hmrc_exchange_rates.py`, because the default test paths do not include `plugin_packages/`.
