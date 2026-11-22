"""Provider call hardening: isolation between plugins, exact answer types, the circuit and the rate limit
(docs/plugin-system.md section 18.3)."""

from __future__ import annotations

import threading
import time
from datetime import datetime, timedelta, timezone

from tests.plugin_host.helpers import base_manifest, install, write_plugin
from tests.plugin_host.test_provider_kinds import (  # noqa: F401 - shared helpers of the provider tests
    TAX,
    VALIDATOR,
    failures,
    setup_plugin,
    tax_call,
    tax_request,
    validator_call,
)

import pytest

from app.plugin_host import health, kinds, providers, runtime
from app.plugin_host.providers import ProviderUnavailable, call_provider

SAME_ID_CODE = """
from datetime import datetime, timezone

from rackflow_plugin.kinds import TaxIdCheckResult, TaxProviderPart, TaxProviderResult


class Validator:
    def supports(self, country, id_type):
        return True

    def check_format(self, country, tax_id):
        return True

    def check_online(self, request):
        return TaxIdCheckResult(status="valid", checked_at=datetime(2026, 11, 22, tzinfo=timezone.utc))


class Tax:
    def supports(self, request):
        return True

    def calculate(self, request):
        return TaxProviderResult(
            lines={line.key: (TaxProviderPart("Zero", "0.0000", "XX", line.net_cents, 0),) for line in request.lines}
        )


def register(plugin):
    plugin.provider("tax_id_validator", "same")(Validator)
    plugin.provider("tax_provider", "same")(Tax)
"""


NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


class AlwaysEqual(str):
    def __eq__(self, other):
        return True

    __hash__ = str.__hash__


class Flipping(kinds.TaxProviderPart):
    """Shows 200 the first time tax_cents is read and 0 after."""

    def __getattribute__(self, name):
        if name == "tax_cents":
            reads = object.__getattribute__(self, "__dict__").setdefault("_reads", [])
            reads.append(1)
            return 200 if len(reads) == 1 else 0
        return object.__getattribute__(self, name)


class SneakyInt(int):
    pass


def calc_request(keys=("a",)):
    return tax_request(lines=tuple(kinds.TaxProviderLine(k, 1000, "standard", "service", None) for k in keys))


def validate_calc(result, keys=("a",)):
    return kinds.validate_answer("tax_provider", "calculate", (calc_request(keys),), result)


def good_part(**changes):
    values = dict(name="VAT", rate_percent="20.0000", jurisdiction="GB", base_cents=1000, tax_cents=200)
    values.update(changes)
    return kinds.TaxProviderPart(**values)


def test_a_status_that_only_claims_to_be_equal_is_refused():
    with pytest.raises(kinds.InvalidAnswer, match="unknown status"):
        kinds.validate_answer("tax_id_validator", "check_online", (), kinds.TaxIdCheckResult(AlwaysEqual("maybe"), NOW))


def test_a_part_subclass_is_refused():
    part = Flipping("VAT", "20.0000", "GB", 1000, 200)
    with pytest.raises(kinds.InvalidAnswer, match="not a TaxProviderPart"):
        validate_calc(kinds.TaxProviderResult(lines={"a": (part,)}))


@pytest.mark.parametrize(
    "changes",
    [{"base_cents": SneakyInt(1000)}, {"tax_cents": SneakyInt(200)}, {"name": AlwaysEqual("VAT")},
     {"rate_percent": AlwaysEqual("20.0000")}],
)  # fmt: skip
def test_subclassed_builtins_in_a_part_are_refused(changes):
    with pytest.raises(kinds.InvalidAnswer):
        validate_calc(kinds.TaxProviderResult(lines={"a": (good_part(**changes),)}))


def test_subclassed_containers_and_results_are_refused():
    class Lines(dict):
        pass

    class Result(kinds.TaxProviderResult):
        pass

    class Check(kinds.TaxIdCheckResult):
        pass

    with pytest.raises(kinds.InvalidAnswer, match="dict by line key"):
        validate_calc(kinds.TaxProviderResult(lines=Lines(a=(good_part(),))))
    with pytest.raises(kinds.InvalidAnswer, match="must return a TaxProviderResult"):
        validate_calc(Result(lines={"a": (good_part(),)}))
    with pytest.raises(kinds.InvalidAnswer, match="must return a TaxIdCheckResult"):
        kinds.validate_answer("tax_id_validator", "check_online", (), Check("valid", NOW))
    with pytest.raises(kinds.InvalidAnswer, match="line keys must be text"):
        validate_calc(kinds.TaxProviderResult(lines={AlwaysEqual("a"): (good_part(),)}))
    with pytest.raises(kinds.InvalidAnswer):
        kinds.validate_answer("tax_rule_pack", "versions", (), [AlwaysEqual("1")])


def test_answers_are_new_objects_of_the_exact_types():
    part = good_part()
    result = kinds.TaxProviderResult(lines={"a": (part,)}, reference="R")
    out = validate_calc(result)
    assert type(out) is kinds.TaxProviderResult and out is not result
    assert type(out.lines["a"][0]) is kinds.TaxProviderPart and out.lines["a"][0] is not part
    assert out.lines["a"][0] == part
    check = kinds.TaxIdCheckResult("valid", NOW, request_ref="R")
    checked = kinds.validate_answer("tax_id_validator", "check_online", (), check)
    assert type(checked) is kinds.TaxIdCheckResult and checked is not check and checked == check
    assert type(checked.checked_at) is datetime and checked.checked_at.tzinfo is timezone.utc


def test_amounts_must_fit_a_big_integer():
    biggest = 2**63 - 1
    ok = good_part(base_cents=biggest, tax_cents=0, rate_percent="0.0000")
    assert validate_calc(kinds.TaxProviderResult(lines={"a": (ok,)})).lines["a"][0].base_cents == biggest
    for changes in ({"base_cents": 10**40, "tax_cents": 0, "rate_percent": "0.0000"}, {"base_cents": biggest + 1, "tax_cents": 0, "rate_percent": "0.0000"}):
        with pytest.raises(kinds.InvalidAnswer, match="larger than"):
            validate_calc(kinds.TaxProviderResult(lines={"a": (good_part(**changes),)}))


@pytest.mark.parametrize("rate", ["20\n", "20.0000\n", "\uff12\uff10.0000", "\u0662\u0660", "20.\u0660"])
def test_rates_are_ascii_digits_with_nothing_after(rate):
    with pytest.raises(kinds.InvalidAnswer, match="rate"):
        validate_calc(kinds.TaxProviderResult(lines={"a": (good_part(rate_percent=rate),)}))


def test_checked_at_must_be_near_now():
    def check(moment):
        return kinds.validate_answer("tax_id_validator", "check_online", (), kinds.TaxIdCheckResult("valid", moment))

    now = datetime.now(timezone.utc)
    assert check(now + timedelta(hours=1)).checked_at == now + timedelta(hours=1)
    assert check(datetime(2000, 1, 1, tzinfo=timezone.utc)).checked_at.year == 2000
    for moment in (datetime(9999, 1, 1, tzinfo=timezone.utc), now + timedelta(days=2), datetime(1999, 12, 31, tzinfo=timezone.utc)):
        with pytest.raises(kinds.InvalidAnswer, match="checked_at"):
            check(moment)


@pytest.mark.parametrize("number", [float("nan"), float("inf"), float("-inf")])
def test_a_pack_with_nan_or_infinity_is_not_json(number):
    with pytest.raises(kinds.InvalidAnswer, match="not JSON"):
        kinds.validate_answer("tax_rule_pack", "load", ("1",), {"rules": [{"rate": number}]})


def test_cheap_answers_do_not_reset_the_circuit(plugin_env, db_session):
    from app.models.plugin_host import PluginInstallation
    from app.plugin_host import health

    setup_plugin(plugin_env, db_session, VALIDATOR, mode="raise")
    for _ in range(health.FAILURES_TO_OPEN):
        assert validator_call("supports", "DE", "eu_vat") is True
        with pytest.raises(ProviderUnavailable) as err:
            validator_call()
        assert err.value.reason == "error"
    assert failures(db_session, VALIDATOR) == health.FAILURES_TO_OPEN
    db_session.expire_all()
    assert health.circuit_open(db_session.get(PluginInstallation, VALIDATOR))
    with pytest.raises(ProviderUnavailable) as err:
        validator_call("supports", "DE", "eu_vat")
    assert err.value.reason == "circuit_open"


def test_the_answer_is_checked_against_billings_own_request(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, TAX, mode="shrink_request")
    request = tax_request()
    with pytest.raises(ProviderUnavailable) as err:
        tax_call(request)
    assert err.value.reason == "invalid_answer"
    assert [line.key for line in request.lines] == ["a", "b"]
    assert failures(db_session, TAX) == 1


def test_an_exception_that_cannot_be_shown_still_counts(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="unprintable_value_error")
    for _ in range(3):
        with pytest.raises(ProviderUnavailable) as err:
            validator_call()
        assert err.value.reason == "error"
    assert failures(db_session, VALIDATOR) == 3


def test_a_keyboard_interrupt_from_an_exception_message_does_not_escape(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="unprintable_interrupt")
    try:
        with pytest.raises(ProviderUnavailable) as err:
            validator_call()
    except KeyboardInterrupt:
        pytest.fail("a KeyboardInterrupt raised by the plugin's exception reached the caller")
    assert err.value.reason == "error"
    assert failures(db_session, VALIDATOR) == 1


def wait_idle(plugin_id, seconds=10.0):
    deadline = time.monotonic() + seconds
    while getattr(providers, "_running", {}).get(plugin_id) and time.monotonic() < deadline:
        time.sleep(0.02)


@pytest.fixture
def release(monkeypatch, tmp_path):
    """A file whose creation lets every call of the validator's ``block`` mode return."""
    flag = tmp_path / "release"
    monkeypatch.setenv("RF_TEST_RELEASE", str(flag))
    yield flag
    flag.touch()
    wait_idle(VALIDATOR)


def test_a_hung_provider_neither_delays_nor_blames_other_plugins(plugin_env, db_session, release, monkeypatch):
    sessions = []
    real_session = providers.new_session

    def tracked_session():
        session = real_session()
        real_close = session.close

        def close():
            session.info["closed"] = True
            real_close()

        session.close = close
        sessions.append(session)
        return session

    monkeypatch.setattr(providers, "new_session", tracked_session)
    monkeypatch.setattr(health, "FAILURES_TO_OPEN", 50)  # so the circuit does not stop the hung calls first
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="block")
    setup_plugin(plugin_env, db_session, TAX)
    reasons = []
    for _ in range(8):
        with pytest.raises(ProviderUnavailable) as err:
            validator_call(timeout_seconds=0.1)
        reasons.append(err.value.reason)
    other = []
    for _ in range(5):
        try:
            other.append(tax_call(timeout_seconds=0.5).reference)
        except ProviderUnavailable as exc:
            other.append(exc.reason)
    assert other == ["TX-1"] * 5, "another plugin's calls are neither delayed nor failed"
    assert failures(db_session, TAX) == 0
    assert reasons == ["timeout", "timeout"] + ["busy"] * 6
    assert failures(db_session, VALIDATOR) == 8, "busy behind calls that all timed out counts"
    assert providers._running[VALIDATOR] == 2 and providers._abandoned[VALIDATOR] == 2

    release.touch()
    wait_idle(VALIDATOR)
    assert VALIDATOR not in providers._running and VALIDATOR not in providers._abandoned
    assert sessions and all(s.info.get("closed") for s in sessions), "every session, abandoned calls' too, is closed"
    assert validator_call("supports", "DE", "eu_vat") is True, "the slots are free again"


def test_a_provider_that_hangs_for_good_opens_its_circuit(plugin_env, db_session, release):
    from sqlalchemy import select

    from app.models.commerce_audit import UserAuditEvent
    from app.models.plugin_host import PluginInstallation

    setup_plugin(plugin_env, db_session, VALIDATOR, mode="block")
    setup_plugin(plugin_env, db_session, TAX)
    reasons = []
    for _ in range(health.FAILURES_TO_OPEN + 2):
        try:
            validator_call(timeout_seconds=0.1)
        except ProviderUnavailable as exc:
            reasons.append(exc.reason)
        assert tax_call(timeout_seconds=0.5).reference == "TX-1", "the healthy plugin is unaffected"
    opened = health.FAILURES_TO_OPEN
    assert reasons == ["timeout", "timeout"] + ["busy"] * (opened - 2) + ["circuit_open"] * 2
    assert failures(db_session, VALIDATOR) == opened
    assert failures(db_session, TAX) == 0
    db_session.expire_all()
    assert health.circuit_open(db_session.get(PluginInstallation, VALIDATOR))
    audits = db_session.execute(
        select(UserAuditEvent.event_metadata).where(UserAuditEvent.action == "plugin.provider_failed")
    ).scalars().all()
    assert len(audits) == 1 and audits[0]["provider"] == "tax_id_validator:fixture"


BLOCK_CODE = """
import os
import time
from datetime import datetime, timezone

from rackflow_plugin.kinds import TaxIdCheckResult


class Validator:
    def supports(self, country, id_type):
        return True

    def check_format(self, country, tax_id):
        return True

    def check_online(self, request):
        deadline = time.monotonic() + 30
        while not os.path.exists(os.environ.get("RF_TEST_RELEASE", "")) and time.monotonic() < deadline:
            time.sleep(0.02)
        return TaxIdCheckResult(status="valid", checked_at=datetime.now(timezone.utc))


def register(plugin):
    plugin.provider("tax_id_validator", "slow")(Validator)
"""


def test_two_calls_in_flight_make_a_third_busy_without_a_failure(plugin_env, db_session, release):
    # A provider that writes no plugin state, so the two calls in flight do not share a write on
    # the test database's single connection.
    manifest = base_manifest(
        rackflow_api=">=1.1,<2",
        permissions=["tax.validate_id"],
        provides=[{"kind": "tax_id_validator", "id": "slow", "label": "S", "id_types": ["eu_vat"], "timeout_seconds": 20}],
    )
    write_plugin(plugin_env, manifest, BLOCK_CODE)
    install(db_session, "demo_plugin")
    request = kinds.TaxIdCheckRequest("DE", "1", "eu_vat")
    results = []

    def slow_call():
        return call_provider("tax_id_validator", "demo_plugin:slow", "check_online", request)

    def in_flight():
        try:
            results.append(slow_call().status)
        except ProviderUnavailable as exc:
            results.append(exc.reason)

    callers = [threading.Thread(target=in_flight, daemon=True) for _ in range(2)]
    for caller in callers:
        caller.start()
    deadline = time.monotonic() + 10
    while providers._running.get("demo_plugin", 0) < 2 and time.monotonic() < deadline:
        time.sleep(0.02)
    assert providers._running.get("demo_plugin") == 2 and not providers._abandoned.get("demo_plugin")
    for _ in range(3):
        with pytest.raises(ProviderUnavailable) as err:
            slow_call()
        assert err.value.reason == "busy"
    assert failures(db_session, "demo_plugin") == 0, "busy behind calls inside their limit is not a failure"
    release.touch()
    for caller in callers:
        caller.join(20)
    assert results == ["valid", "valid"]
    assert failures(db_session, "demo_plugin") == 0


SLOW_IMPORT_CODE = """
import os
import time

if os.environ.get("RF_TEST_SLOW_IMPORT"):
    time.sleep(3)
""" + SAME_ID_CODE.replace('    plugin.provider("tax_provider", "same")(Tax)\n', "")


def test_loading_the_plugin_counts_within_the_call_time_limit(plugin_env, db_session, monkeypatch):
    manifest = base_manifest(
        rackflow_api=">=1.1,<2",
        permissions=["tax.validate_id"],
        provides=[{"kind": "tax_id_validator", "id": "same", "label": "V", "id_types": ["eu_vat"], "timeout_seconds": 1}],
    )
    write_plugin(plugin_env, manifest, SLOW_IMPORT_CODE)
    install(db_session, "demo_plugin")
    runtime.reset_all()  # a fresh process: the first call imports the plugin
    monkeypatch.setenv("RF_TEST_SLOW_IMPORT", "1")
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable) as err:
        call_provider("tax_id_validator", "demo_plugin:same", "supports", "DE", "eu_vat")
    assert err.value.reason == "timeout"
    assert time.monotonic() - started < 2.0
    assert failures(db_session, "demo_plugin") == 1


def test_a_redis_that_does_not_answer_cannot_hold_the_caller(plugin_env, db_session, monkeypatch):
    import socket

    import redis

    import app.core.redis as redis_module
    from app.plugin_host import common

    setup_plugin(plugin_env, db_session, VALIDATOR, rate_limit={"calls": 2, "per_seconds": 3600})
    silent = socket.socket()
    silent.bind(("127.0.0.1", 0))
    silent.listen(16)  # connections are accepted by the kernel and never answered
    shared = redis.Redis(host="127.0.0.1", port=silent.getsockname()[1], decode_responses=True)
    monkeypatch.setattr(redis_module, "redis_client", shared)
    common._local_counters.clear()
    outcome = []

    def calls():
        for _ in range(3):
            try:
                outcome.append(validator_call().status)
            except ProviderUnavailable as exc:
                outcome.append(exc.reason)

    caller = threading.Thread(target=calls, daemon=True)
    started = time.monotonic()
    caller.start()
    caller.join(5)
    try:
        assert not caller.is_alive(), "the caller was still blocked on Redis after 5 s"
        assert outcome == ["valid", "valid", "rate_limited"]
        assert time.monotonic() - started < 3 * (2 * providers.REDIS_TIMEOUT_SECONDS + 0.5)
        assert common._local_counters, "a silent Redis falls back to the per-process count"
        assert redis_module.redis_client is shared and shared.connection_pool.connection_kwargs.get("socket_timeout") is None
    finally:
        silent.close()
        caller.join(10)


def test_two_kinds_with_the_same_provider_id_have_separate_rate_limits(plugin_env, db_session):
    limit = {"calls": 1, "per_seconds": 3600}
    manifest = base_manifest(
        rackflow_api=">=1.1,<2",
        permissions=["tax.validate_id", "tax.calculate"],
        provides=[
            {"kind": "tax_id_validator", "id": "same", "label": "V", "id_types": ["eu_vat"], "rate_limit": limit},
            {"kind": "tax_provider", "id": "same", "label": "T", "rate_limit": limit},
        ],
    )
    write_plugin(plugin_env, manifest, SAME_ID_CODE)
    install(db_session, "demo_plugin")
    request = kinds.TaxIdCheckRequest("DE", "1", "eu_vat")
    assert call_provider("tax_id_validator", "demo_plugin:same", "check_online", request).status == "valid"
    assert set(call_provider("tax_provider", "demo_plugin:same", "calculate", tax_request()).lines) == {"a", "b"}
    with pytest.raises(ProviderUnavailable) as err:
        call_provider("tax_provider", "demo_plugin:same", "calculate", tax_request())
    assert err.value.reason == "rate_limited"
