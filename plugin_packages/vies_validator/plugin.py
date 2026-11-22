"""Checks EU VAT numbers with the European Commission's VIES REST service."""

import json
import re
from datetime import datetime, timedelta, timezone

from rackflow_plugin.kinds import TaxIdCheckResult, current_context

PROVIDER_ID = "vies"
ID_TYPE = "eu_vat"
HTTP_TIMEOUT = 6
BACKOFF = timedelta(minutes=5)

# Number formats published by VIES, without the country prefix. Greece is EL, not GR.
FORMATS = {
    "AT": r"U[0-9]{8}",
    "BE": r"[01][0-9]{9}",
    "BG": r"[0-9]{9,10}",
    "CY": r"[0-9]{8}[A-Z]",
    "CZ": r"[0-9]{8,10}",
    "DE": r"[0-9]{9}",
    "DK": r"[0-9]{8}",
    "EE": r"[0-9]{9}",
    "EL": r"[0-9]{9}",
    "ES": r"[0-9A-Z][0-9]{7}[0-9A-Z]",
    "FI": r"[0-9]{8}",
    "FR": r"[0-9A-Z]{2}[0-9]{9}",
    "HR": r"[0-9]{11}",
    "HU": r"[0-9]{8}",
    "IE": r"[0-9][A-Z0-9+*][0-9]{5}[A-Z]{1,2}",
    "IT": r"[0-9]{11}",
    "LT": r"([0-9]{9}|[0-9]{12})",
    "LU": r"[0-9]{8}",
    "LV": r"[0-9]{11}",
    "MT": r"[0-9]{8}",
    "NL": r"[0-9]{9}B[0-9]{2}",
    "PL": r"[0-9]{10}",
    "PT": r"[0-9]{9}",
    "RO": r"[0-9]{2,10}",
    "SE": r"[0-9]{12}",
    "SI": r"[0-9]{8}",
    "SK": r"[0-9]{10}",
}
PATTERNS = {country: re.compile(f"^{pattern}$") for country, pattern in FORMATS.items()}

# Errors that say nothing about the number: the service, the member state or the requester is the problem.
UNAVAILABLE = {
    "MS_UNAVAILABLE",
    "SERVICE_UNAVAILABLE",
    "TIMEOUT",
    "MS_MAX_CONCURRENT_REQ",
    "MS_MAX_CONCURRENT_REQ_TIME",
    "GLOBAL_MAX_CONCURRENT_REQ",
    "GLOBAL_MAX_CONCURRENT_REQ_TIME",
    "INVALID_REQUESTER_INFO",
    "VAT_BLOCKED",
    "IP_BLOCKED",
}
INVALID = {"INVALID_INPUT"}
NO_DETAIL = {"", "---"}
# VIES's requestDate is used as the check time only when it is this close to the plugin's own clock.
REQUEST_DATE_WINDOW = timedelta(minutes=15)


def _clean(value):
    """A detail from the answer: text only, and None for VIES's placeholders."""
    if not isinstance(value, str):
        return None
    text = value.strip()
    return None if text in NO_DETAIL else text


def _code(value):
    """An error code that fits the 64 character column and holds nothing but code characters."""
    text = re.sub(r"[^A-Za-z0-9_]", "_", value if isinstance(value, str) else "").strip("_").upper()
    return text[:64] or "UNKNOWN"


def _parse_time(text, fallback):
    if not isinstance(text, str):
        return fallback
    try:
        moment = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return fallback
    return moment if moment.tzinfo else moment.replace(tzinfo=timezone.utc)


def _checked_at(request_date, now):
    """VIES's own time of the check when it is close to ``now``, else ``now``."""
    moment = _parse_time(request_date, now)
    return moment if abs(moment - now) <= REQUEST_DATE_WINDOW else now


def _normalised(value):
    return re.sub(r"[\s.\-]", "", value).upper() if isinstance(value, str) else None


def _answers_the_request(data, request):
    """Whether the answer is about the number that was asked about."""
    country = _normalised(data.get("countryCode"))
    number = _normalised(data.get("vatNumber"))
    asked = _normalised(request.tax_id)
    if country != _normalised(request.country) or number is None or asked is None:
        return False
    return number in (asked, country + asked) or asked == country + number


def unavailable(now, code):
    return TaxIdCheckResult(status="unavailable", checked_at=now, error_code=_code(code))


def _backoff_key(country):
    return f"backoff_{country.lower()}"


def _in_backoff(ctx, country, now):
    until = ctx.store.get(_backoff_key(country))
    if not until:
        return False
    return _parse_time(until, now) > now


def _requester(ctx, request):
    """The requester's country and number: the request's, else the plugin settings; both or neither."""
    country = request.requester_country or ctx.settings.get("requester_country")
    number = request.requester_tax_id or ctx.settings.get("requester_vat_number")
    if not country or not number:
        return None, None
    return str(country).upper(), str(number).replace(" ", "").upper()


def interpret(ctx, request, status, text, now):
    """Turn one VIES answer into a result. ``status`` is the HTTP status, ``text`` the body."""
    try:
        data = json.loads(text)
    except (TypeError, ValueError, RecursionError):  # RecursionError: nested too deeply to read
        data = None
    if not isinstance(data, dict):
        return unavailable(now, f"HTTP_{status}" if status != 200 else "BAD_RESPONSE")

    wrappers = data.get("errorWrappers")
    if data.get("actionSucceed") is False or wrappers:
        first = wrappers[0] if isinstance(wrappers, list) and wrappers and isinstance(wrappers[0], dict) else {}
        code = _code(first.get("error") or f"HTTP_{status}")
        if code in INVALID:
            return TaxIdCheckResult(status="invalid", checked_at=now, error_code=code)
        if code == "INVALID_REQUESTER_INFO":
            ctx.log.error(
                "VIES refused the requester details; check Your VAT country and Your VAT number in the plugin settings"
            )
        if code == "MS_UNAVAILABLE":
            ctx.store.set(_backoff_key(request.country), (now + BACKOFF).isoformat())
        return unavailable(now, code)

    if status != 200 or not isinstance(data.get("valid"), bool):
        return unavailable(now, f"HTTP_{status}" if status != 200 else "BAD_RESPONSE")

    if not _answers_the_request(data, request):
        return unavailable(now, "MISMATCHED_RESPONSE")
    checked_at = _checked_at(data.get("requestDate"), now)
    if not data["valid"]:
        return TaxIdCheckResult(status="invalid", checked_at=checked_at)
    return TaxIdCheckResult(
        status="valid",
        checked_at=checked_at,
        request_ref=_clean(data.get("requestIdentifier")),
        registered_name=_clean(data.get("name")),
        registered_address=_clean(data.get("address")),
    )


def register(plugin):
    @plugin.provider("tax_id_validator", PROVIDER_ID)
    class Vies:
        def supports(self, country, id_type):
            return id_type == ID_TYPE and country in PATTERNS

        def check_format(self, country, tax_id):
            pattern = PATTERNS.get(country)
            return bool(pattern and isinstance(tax_id, str) and pattern.match(tax_id))

        async def check_online(self, request):
            ctx = current_context()
            now = ctx.now()
            if not self.supports(request.country, request.id_type):
                return unavailable(now, "UNSUPPORTED")
            if _in_backoff(ctx, request.country, now):
                return unavailable(now, "MS_UNAVAILABLE")

            body = {"countryCode": request.country, "vatNumber": request.tax_id}
            country, number = _requester(ctx, request)
            if country:
                body["requesterMemberStateCode"] = country
                body["requesterNumber"] = number
            try:
                reply = await ctx.http.post(
                    str(ctx.settings.get("endpoint")), json=body, headers={"Accept": "application/json"}, timeout=HTTP_TIMEOUT
                )
            except Exception:  # noqa: BLE001 - every network failure is "VIES could not be asked"
                return unavailable(now, "NETWORK_ERROR")
            return interpret(ctx, request, reply["status"], reply["text"], now)
