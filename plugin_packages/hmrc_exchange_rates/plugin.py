"""Reads HMRC's monthly exchange rates (currency units per GBP 1) from the UK Trade Tariff service."""

import re
import xml.etree.ElementTree as ElementTree
from datetime import date, datetime, timedelta

from rackflow_plugin.kinds import CurrencyRate, CurrencyRatesResult, current_context

PROVIDER_ID = "hmrc_monthly"
SOURCE = "hmrc_monthly"
BASE = "GBP"
HTTP_TIMEOUT = 12
PATH = "/api/v2/exchange_rates/files/monthly_xml_{year}-{month}.xml"

_PERIOD_RE = re.compile(r"^([0-9]{4})-(0[1-9]|1[0-2])$")
_CURRENCY_RE = re.compile(r"^[A-Z]{3}$")
_RATE_RE = re.compile(r"^[0-9]{1,12}(\.[0-9]{1,6})?$")
# The DOCTYPE check refuses entity declarations, so nothing in the file is expanded.
_DOCTYPE_RE = re.compile(r"<!\s*(DOCTYPE|ENTITY)", re.IGNORECASE)


class HmrcError(Exception):
    """HMRC's answer could not be used. The call fails, and billing asks again later."""


def month_bounds(period):
    match = _PERIOD_RE.match(period or "")
    if not match:
        raise HmrcError(f"not a YYYY-MM period: {period!r}")
    year, month = int(match.group(1)), int(match.group(2))
    following = date(year + month // 12, month % 12 + 1, 1)
    return date(year, month, 1), following - timedelta(days=1)


def file_url(base_url, start):
    """The month's file; HMRC does not zero pad the month (``monthly_xml_2026-9.xml``)."""
    return str(base_url).rstrip("/") + PATH.format(year=start.year, month=start.month)


def _period_attribute(text):
    """``01/Nov/2026 to 30/Nov/2026`` as two dates."""
    try:
        first, last = (part.strip() for part in str(text).split(" to "))
        return datetime.strptime(first, "%d/%b/%Y").date(), datetime.strptime(last, "%d/%b/%Y").date()
    except ValueError as exc:
        raise HmrcError(f"unreadable Period attribute {text!r}") from exc


def parse(text, start, end, log=None):
    """The rates in one monthly file, after checking it is the file of ``start`` to ``end``."""
    if not isinstance(text, str) or _DOCTYPE_RE.search(text):
        raise HmrcError("the file is not plain XML")
    try:
        root = ElementTree.fromstring(text.encode("utf-8"))
    except ElementTree.ParseError as exc:
        raise HmrcError(f"unparseable XML: {exc}") from exc
    if root.tag != "exchangeRateMonthList":
        raise HmrcError(f"unexpected root element {root.tag!r}")
    if _period_attribute(root.get("Period")) != (start, end):
        raise HmrcError(f"the file is for {root.get('Period')!r}, not {start.isoformat()} to {end.isoformat()}")

    found, conflicting = {}, set()
    for entry in root.iter("exchangeRate"):
        code = (entry.findtext("currencyCode") or "").strip().upper()
        rate = (entry.findtext("rateNew") or "").strip()
        if not _CURRENCY_RE.match(code) or code == BASE or not _RATE_RE.match(rate) or float(rate) <= 0:
            continue
        if code in found and found[code] != rate:
            conflicting.add(code)
        found.setdefault(code, rate)
    for code in sorted(conflicting):
        found.pop(code)
        if log is not None:
            log.warning(f"HMRC lists {code} with different rates for {start:%Y-%m}; it is left out")
    return tuple(CurrencyRate(currency=code, units_per_base=rate) for code, rate in sorted(found.items()))


def register(plugin):
    @plugin.provider("currency_rates", PROVIDER_ID)
    class HmrcMonthly:
        async def rates(self, request):
            start, end = month_bounds(request.period)
            if request.base != BASE:
                return CurrencyRatesResult(source=SOURCE, period_start=start, period_end=end, rates=())
            ctx = current_context()
            url = file_url(ctx.settings.get("base_url"), start)
            reply = await ctx.http.get(url, headers={"Accept": "application/xml"}, timeout=HTTP_TIMEOUT)
            status = reply["status"]
            if status == 404:
                return CurrencyRatesResult(source=SOURCE, period_start=start, period_end=end, rates=())
            if status != 200:
                raise HmrcError(f"HMRC answered HTTP {status}")
            rates = parse(reply["text"], start, end, ctx.log)
            return CurrencyRatesResult(source=SOURCE, period_start=start, period_end=end, rates=rates)
