"""Plugin API 1.1 provider kinds: manifest, registration, the call path and bundled discovery
(docs/billing-phase2-design.md section 8)."""

from __future__ import annotations

import json
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import pytest
from sqlalchemy import select

from app.core.config import settings
from app.models.commerce_audit import UserAuditEvent
from app.models.plugin_host import PluginInstallation, PluginState, PluginStatus
from app.plugin_host import common, health, kinds, lifecycle, providers, runtime, settings_store
from app.plugin_host.loader import PluginLoadError, discover, find_package, import_package, read_package
from app.plugin_host.manifest import ManifestError, validate_manifest
from app.plugin_host.providers import ProviderUnavailable, call_provider, providers_of
from tests.plugin_host.helpers import base_manifest, install, run, write_plugin

FIXTURES = Path(__file__).parent / "fixtures"
VALIDATOR = "fixture_tax_id_validator"
PACK = "fixture_tax_rule_pack"
TAX = "fixture_tax_provider"

NOW = datetime(2026, 11, 22, 12, 0, tzinfo=timezone.utc)


# ------------------------------------------------------------------ helpers


def put_fixture(root: Path, name: str, *, provides: dict | None = None, **manifest_changes) -> Path:
    """Copy a fixture plugin into ``root``; ``provides`` is merged into its first provider entry."""
    target = root / name
    shutil.copytree(FIXTURES / name, target, ignore=shutil.ignore_patterns("__pycache__"))
    manifest = json.loads((target / "plugin.json").read_text())
    manifest.update(manifest_changes)
    if provides is not None:
        entry = manifest["provides"][0]
        for key, value in provides.items():
            if value is None:
                entry.pop(key, None)
            else:
                entry[key] = value
    (target / "plugin.json").write_text(json.dumps(manifest))
    return target


def setup_plugin(plugin_env, db, name, *, mode=None, provides=None, rate_limit=None, **manifest_changes):
    """Install and enable a fixture plugin. The validator's rate limit is off unless asked for."""
    changes = dict(provides or {})
    if name == VALIDATOR:
        changes.setdefault("rate_limit", rate_limit)
    put_fixture(plugin_env, name, provides=changes, **manifest_changes)
    inst = install(db, name)
    if mode is not None:
        manifest = runtime.manifest_of(inst)
        settings_store.update(db, name, manifest, {"mode": mode})
        db.commit()
    return inst


def validator_call(method="check_online", *args, ref=f"{VALIDATOR}:fixture", **kw):
    if method == "check_online" and not args:
        args = (kinds.TaxIdCheckRequest("DE", "123456789", "eu_vat", None, None),)
    return call_provider("tax_id_validator", ref, method, *args, **kw)


def tax_request(**changes):
    values = dict(
        tax_point_at=NOW,
        currency="EUR",
        prices_include_tax=False,
        seller={"registrations": []},
        buyer={"country": "XX", "is_business": False},
        lines=(kinds.TaxProviderLine("a", 1000, "standard", "service", None), kinds.TaxProviderLine("b", 505, "standard", "service", "P")),
    )
    values.update(changes)
    return kinds.TaxProviderRequest(**values)


def tax_call(request=None, method="calculate", **kw):
    return call_provider("tax_provider", f"{TAX}:flat", method, request or tax_request(), **kw)


def failures(db, plugin_id):
    db.expire_all()
    return db.get(PluginInstallation, plugin_id).consecutive_failures


def audit_actions(db):
    db.expire_all()
    return [e for e in db.execute(select(UserAuditEvent)).scalars()]


# ------------------------------------------------------------------ the SDK module


def test_rackflow_plugin_kinds_is_the_host_module():
    import rackflow_plugin.kinds as sdk_kinds
    from rackflow_plugin.kinds import TaxIdCheckRequest

    assert sdk_kinds is kinds
    assert TaxIdCheckRequest is kinds.TaxIdCheckRequest
    with pytest.raises(RuntimeError):
        sdk_kinds.current_context()


def test_the_data_types_are_frozen():
    request = kinds.TaxIdCheckRequest("DE", "1", "eu_vat", None, None)
    with pytest.raises(Exception):
        request.country = "FR"  # type: ignore[misc]


# ------------------------------------------------------------------ manifest


def provide(**changes):
    entry = {"kind": "tax_id_validator", "id": "vies", "label": "VIES", "id_types": ["eu_vat"]}
    entry.update(changes)
    return entry


def manifest_with(*entries, api=">=1.1,<2", permissions=("tax.validate_id",), **extra):
    return base_manifest(rackflow_api=api, permissions=list(permissions), provides=list(entries), **extra)


@pytest.mark.parametrize("spec", ["==1.0", "<1.1", "<=1.0"])
def test_provides_is_refused_when_the_range_allows_only_1_0(spec):
    with pytest.raises(ManifestError, match="provides needs plugin API 1.1; this manifest allows only 1.0"):
        validate_manifest(manifest_with(provide(), api=spec))


@pytest.mark.parametrize("spec", [">=1.1,<2", ">=1.0,<2", ">=1.1", "==1.1", ">=1.0"])
def test_provides_is_accepted_when_the_range_reaches_1_1(spec):
    manifest = validate_manifest(manifest_with(provide(), api=spec))
    assert manifest.effective_api() == "1.1"
    assert manifest.provider("tax_id_validator", "vies").label == "VIES"


def test_a_manifest_without_provides_is_unchanged():
    manifest = validate_manifest(base_manifest(rackflow_api="==1.0"))
    assert manifest.provides == []
    assert manifest.effective_api() == "1.0"


@pytest.mark.parametrize("spec", ["==1.0", ">=1.0", ">=1.0,<2", ">=1.0,<2.0"])
def test_1_0_plugins_load_and_behave_unchanged(plugin_env, db_session, spec):
    code = (
        "def register(plugin):\n"
        "    @plugin.on('service.created')\n"
        "    def handler(ctx, event):\n"
        "        pass\n"
    )
    write_plugin(plugin_env, base_manifest(rackflow_api=spec, permissions=["service.read"], events=["service.created"]), code)
    package = find_package("demo_plugin")
    assert package.valid and not package.bundled
    inst = install(db_session, "demo_plugin")
    assert inst.status == PluginStatus.ENABLED
    assert inst.effective_api == ("1.0" if spec == "==1.0" else "1.1")


@pytest.mark.parametrize(
    "entry, message",
    [
        (provide(kind="nope"), "kind"),
        (provide(id="Bad Id"), "provider id"),
        (provide(id_types=[]), "needs id_types"),
        (provide(countries=["de"]), "upper case ISO alpha-2"),
        (provide(countries=["DEU"]), "upper case ISO alpha-2"),
        (provide(id_types=["Bad"]), "id types"),
        (provide(timeout_seconds=31), "timeout_seconds"),
        (provide(timeout_seconds=0), "timeout_seconds"),
        (provide(rate_limit={"calls": 0, "per_seconds": 60}), "calls"),
        (provide(rate_limit={"calls": 10001, "per_seconds": 60}), "calls"),
        (provide(rate_limit={"calls": 1, "per_seconds": 86401}), "per_seconds"),
        (provide(currencies=["EUR"]), "currencies belong to tax_provider"),
        (provide(pack={"file": "packs/x.json"}), "pack belongs to tax_rule_pack"),
        (provide(label=""), "label"),
        (provide(unknown_key=1), "unknown_key"),
    ],
)
def test_provide_spec_validation(entry, message):
    with pytest.raises(ManifestError, match=message):
        validate_manifest(manifest_with(entry))


def test_each_kind_needs_its_permission():
    for kind, permission, extra in (
        ("tax_id_validator", "tax.validate_id", {"id_types": ["eu_vat"]}),
        ("tax_rule_pack", "tax.rule_pack", {"pack": {"file": "p.json"}}),
        ("tax_provider", "tax.calculate", {}),
    ):
        entry = {"kind": kind, "id": "one", "label": "One", **extra}
        validate_manifest(manifest_with(entry, permissions=(permission,)))
        with pytest.raises(ManifestError, match=f"needs the {permission} permission"):
            validate_manifest(manifest_with(entry, permissions=()))


def test_tax_provider_rejects_id_types_and_rule_pack_needs_a_pack():
    with pytest.raises(ManifestError, match="id_types belong to tax_id_validator"):
        validate_manifest(manifest_with({"kind": "tax_provider", "id": "p", "label": "P", "id_types": ["x"]}, permissions=("tax.calculate",)))
    with pytest.raises(ManifestError, match="needs pack.file"):
        validate_manifest(manifest_with({"kind": "tax_rule_pack", "id": "p", "label": "P"}, permissions=("tax.rule_pack",)))


@pytest.mark.parametrize("path", ["../x.json", "/abs.json", "a//b.json", "pack.txt", "a\\b.json"])
def test_pack_file_must_be_a_relative_json_path(path):
    entry = {"kind": "tax_rule_pack", "id": "p", "label": "P", "pack": {"file": path}}
    with pytest.raises(ManifestError, match="pack"):
        validate_manifest(manifest_with(entry, permissions=("tax.rule_pack",)))


def test_provider_ids_are_unique_per_kind_and_at_most_ten():
    with pytest.raises(ManifestError, match="must be unique"):
        validate_manifest(manifest_with(provide(), provide()))
    entries = [provide(id=f"v{i}") for i in range(11)]
    with pytest.raises(ManifestError, match="provides"):
        validate_manifest(manifest_with(*entries))
    validate_manifest(manifest_with(*entries[:10]))
    # the same id under two kinds is fine
    validate_manifest(
        manifest_with(
            provide(id="same"),
            {"kind": "tax_provider", "id": "same", "label": "S"},
            permissions=("tax.validate_id", "tax.calculate"),
        )
    )


def test_a_pack_file_must_exist_and_be_json(plugin_env):
    path = put_fixture(plugin_env, PACK)
    assert find_package(PACK).valid
    (path / "packs" / "demo.json").write_text("{not json")
    assert any("not valid JSON" in e for e in find_package(PACK).errors)
    (path / "packs" / "demo.json").unlink()
    assert any("packs/demo.json of provider demo_pack is missing" in e for e in find_package(PACK).errors)
    (path / "packs" / "demo.json").write_text("[1]")
    assert any("must hold a JSON object" in e for e in find_package(PACK).errors)


# ------------------------------------------------------------------ registration


def register_code(body: str) -> str:
    return "from rackflow_plugin.kinds import TaxIdCheckResult\n\n" + body


GOOD_VALIDATOR = """
class Validator:
    BUILT = []

    def __init__(self):
        Validator.BUILT.append(1)

    def supports(self, country, id_type):
        return True

    def check_format(self, country, tax_id):
        return True

    def check_online(self, request):
        return TaxIdCheckResult(status="valid", checked_at=None)
"""


def import_with(plugin_env, body, *entries, permissions=("tax.validate_id",)):
    write_plugin(plugin_env, manifest_with(*entries, permissions=permissions), register_code(body))
    package = find_package("demo_plugin")
    assert package.valid, package.errors
    return import_package(package)


def test_a_class_provider_is_instantiated_once_per_load(plugin_env):
    loaded = import_with(
        plugin_env,
        GOOD_VALIDATOR + "\ndef register(plugin):\n    plugin.provider('tax_id_validator', 'vies')(Validator)\n",
        provide(),
    )
    obj = loaded.handlers.providers[("tax_id_validator", "vies")]
    assert type(obj).__name__ == "Validator"
    assert len(obj.BUILT) == 1
    assert isinstance(obj, kinds.TaxIdValidator)


def test_a_factory_function_provider(plugin_env):
    code = GOOD_VALIDATOR + (
        "\ndef register(plugin):\n"
        "    @plugin.provider('tax_id_validator', 'vies')\n"
        "    def make():\n"
        "        return Validator()\n"
    )
    loaded = import_with(plugin_env, code, provide())
    assert loaded.handlers.providers[("tax_id_validator", "vies")].supports("DE", "eu_vat") is True


def test_a_declared_provider_without_a_binding_fails_the_load(plugin_env):
    with pytest.raises(PluginLoadError, match=r"provider tax_id_validator:vies is declared but has no @plugin.provider"):
        import_with(plugin_env, "def register(plugin):\n    pass\n", provide())


def test_a_bound_provider_that_is_not_declared_fails_the_load(plugin_env):
    code = GOOD_VALIDATOR + "\ndef register(plugin):\n    plugin.provider('tax_id_validator', 'vies')(Validator)\n"
    with pytest.raises(PluginLoadError, match=r"registered but not declared in provides"):
        import_with(plugin_env, code)


def test_registering_twice_or_an_unknown_kind_fails_the_load(plugin_env):
    twice = GOOD_VALIDATOR + (
        "\ndef register(plugin):\n"
        "    plugin.provider('tax_id_validator', 'vies')(Validator)\n"
        "    plugin.provider('tax_id_validator', 'vies')(Validator)\n"
    )
    with pytest.raises(PluginLoadError, match="registered twice"):
        import_with(plugin_env, twice, provide())
    unknown = "def register(plugin):\n    plugin.provider('payment_gateway', 'x')(object)\n"
    with pytest.raises(PluginLoadError, match="unknown provider kind"):
        import_with(plugin_env, unknown, provide())
    not_callable = "def register(plugin):\n    plugin.provider('tax_id_validator', 'vies')(object())\n"
    with pytest.raises(PluginLoadError, match="not a class or a factory"):
        import_with(plugin_env, not_callable, provide())


@pytest.mark.parametrize(
    "body, message",
    [
        ("class P:\n    def supports(self, c, t): return True\n    def check_format(self, c, t): return True\n", "no method check_online"),
        (
            "class P:\n    def supports(self, c): return True\n    def check_format(self, c, t): return True\n"
            "    def check_online(self, r): return None\n",
            "method supports must accept 2 arguments",
        ),
        (
            "class P:\n    def supports(self, c, t): return True\n    def check_format(self, c, t): return True\n"
            "    def check_online(self): return None\n",
            "method check_online must accept 1 argument",
        ),
        (
            "class P:\n    def supports(self, c, t, extra): return True\n    def check_format(self, c, t): return True\n"
            "    def check_online(self, r): return None\n",
            "method supports must accept 2 arguments",
        ),
        ("class P:\n    supports = 1\n    check_format = 2\n    check_online = 3\n", "no method supports"),
    ],
)
def test_a_provider_must_satisfy_its_protocol(plugin_env, body, message):
    code = body + "\ndef register(plugin):\n    plugin.provider('tax_id_validator', 'vies')(P)\n"
    with pytest.raises(PluginLoadError, match=message):
        import_with(plugin_env, code, provide())


def test_a_constructor_that_raises_fails_the_load(plugin_env):
    code = (
        "class P:\n    def __init__(self):\n        raise RuntimeError('no config')\n"
        "\ndef register(plugin):\n    plugin.provider('tax_id_validator', 'vies')(P)\n"
    )
    with pytest.raises(PluginLoadError, match="could not be created: RuntimeError: no config"):
        import_with(plugin_env, code, provide())


def test_a_failing_provider_load_leaves_the_plugin_failed(plugin_env, db_session):
    put_fixture(plugin_env, VALIDATOR)
    inst = install(db_session, VALIDATOR)
    (plugin_env / VALIDATOR / "plugin.py").write_text("def register(plugin):\n    pass\n")
    assert runtime.load_installed(inst) is None
    db_session.expire_all()
    assert db_session.get(PluginInstallation, VALIDATOR).status == PluginStatus.FILES_CHANGED


# ------------------------------------------------------------------ providers_of


def test_providers_of_lists_enabled_providers_with_a_closed_circuit(plugin_env, db_session):
    assert providers_of("tax_id_validator") == []
    setup_plugin(plugin_env, db_session, VALIDATOR)
    setup_plugin(plugin_env, db_session, PACK)
    refs = providers_of("tax_id_validator")
    assert [(r.plugin_id, r.provider_id, r.kind, r.ref, r.label) for r in refs] == [
        (VALIDATOR, "fixture", "tax_id_validator", f"{VALIDATOR}:fixture", "Fixture validator")
    ]
    assert [r.ref for r in providers_of("tax_rule_pack")] == [f"{PACK}:demo_pack"]
    assert providers_of("tax_provider") == []
    assert providers_of("payment_gateway") == []
    inst = db_session.get(PluginInstallation, VALIDATOR)
    inst.circuit_open_until = common.utcnow().replace(year=2099)
    db_session.commit()
    assert providers_of("tax_id_validator") == []
    health.reset(db_session, inst)
    inst.status = PluginStatus.DISABLED
    db_session.commit()
    assert providers_of("tax_id_validator") == []


# ------------------------------------------------------------------ calls: success


def test_the_validator_answers_and_commits_its_state(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR)
    ref = f"{VALIDATOR}:fixture"
    assert validator_call("supports", "DE", "eu_vat") is True
    assert validator_call("supports", "US", "eu_vat") is False
    assert validator_call("check_format", "DE", "123") is True
    result = validator_call()
    assert isinstance(result, kinds.TaxIdCheckResult)
    assert (result.status, result.request_ref, result.registered_name) == ("valid", "REF-1", "Example GmbH")
    value = db_session.execute(select(PluginState).where(PluginState.key == "last_country")).scalar_one().value
    assert value == "DE"
    assert failures(db_session, VALIDATOR) == 0
    assert ref in [r.ref for r in providers_of("tax_id_validator")]


def test_a_coroutine_method_is_awaited(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="async")
    assert validator_call().request_ref == "ASYNC"


def test_the_pack_provider(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, PACK)
    ref = f"{PACK}:demo_pack"
    assert call_provider("tax_rule_pack", ref, "pack_id") == "demo_pack"
    assert call_provider("tax_rule_pack", ref, "versions") == ["2026.11.1"]
    data = call_provider("tax_rule_pack", ref, "load", "2026.11.1")
    assert data["pack_id"] == "demo_pack" and data["rules"][0]["rate_percent"] == "10.0000"
    with pytest.raises(ProviderUnavailable) as err:
        call_provider("tax_rule_pack", ref, "load", "nope")
    assert err.value.reason == "error"


def test_the_tax_provider(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, TAX)
    request = tax_request()
    assert call_provider("tax_provider", f"{TAX}:flat", "supports", request) is True
    result = tax_call(request)
    assert result.reference == "TX-1"
    assert result.lines["a"][0].tax_cents == 100
    assert result.lines["b"][0].tax_cents == 50  # 10% of 505 is 50.5, within one minor unit
    assert tax_call(tax_request(currency="USD"), "supports") is False


def test_the_plugin_gets_copies_of_the_arguments(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, TAX, mode="mutate_request")
    request = tax_request()
    tax_call(request)
    assert request.buyer["country"] == "XX"


def test_billing_may_lower_the_timeout_never_raise_it(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="hang")
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable) as err:
        validator_call(timeout_seconds=0.2)
    assert err.value.reason == "timeout"
    assert time.monotonic() - started < 0.9
    started = time.monotonic()
    with pytest.raises(ProviderUnavailable):
        validator_call(timeout_seconds=60)  # the provider's own 1 s still applies
    assert 0.9 < time.monotonic() - started < 2.5


# ------------------------------------------------------------------ calls: failure isolation


def test_a_timeout_counts_for_the_circuit_and_discards_the_state(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="hang")
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "timeout"
    assert failures(db_session, VALIDATOR) == 1
    time.sleep(2.3)  # the abandoned call finishes in its thread; it must not commit
    assert db_session.execute(select(PluginState).where(PluginState.key == "last_country")).first() is None


def test_an_exception_becomes_provider_unavailable_and_counts(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="raise")
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "error"
    assert err.value.__cause__ is None
    assert failures(db_session, VALIDATOR) == 1
    db_session.expire_all()
    assert "the registry exploded" in db_session.get(PluginInstallation, VALIDATOR).last_error


def test_a_system_exit_in_a_provider_does_not_unwind_billing(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="sysexit")
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "error"
    assert failures(db_session, VALIDATOR) == 1


@pytest.mark.parametrize("mode", ["bad_status", "no_error_code", "not_a_result"])
def test_invalid_validator_answers_count_as_failures(plugin_env, db_session, mode):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode=mode)
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "invalid_answer"
    assert failures(db_session, VALIDATOR) == 1


@pytest.mark.parametrize(
    "mode",
    ["missing_key", "extra_key", "negative", "wrong_tax", "reverse_charge_with_tax", "float_amount", "bad_rate"],
)
def test_invalid_tax_provider_answers_count_as_failures(plugin_env, db_session, mode):
    setup_plugin(plugin_env, db_session, TAX, mode=mode)
    with pytest.raises(ProviderUnavailable) as err:
        tax_call()
    assert err.value.reason == "invalid_answer"
    assert failures(db_session, TAX) == 1


def test_an_exception_in_the_tax_provider_counts(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, TAX, mode="raise")
    with pytest.raises(ProviderUnavailable) as err:
        tax_call()
    assert err.value.reason == "error"
    assert failures(db_session, TAX) == 1


def test_a_valid_answer_resets_the_failure_count(plugin_env, db_session):
    inst = setup_plugin(plugin_env, db_session, VALIDATOR, mode="raise")
    for _ in range(3):
        with pytest.raises(ProviderUnavailable):
            validator_call()
    assert failures(db_session, VALIDATOR) == 3
    settings_store.update(db_session, VALIDATOR, runtime.manifest_of(inst), {"mode": "valid"})
    db_session.commit()
    validator_call()
    assert failures(db_session, VALIDATOR) == 0


def test_five_failures_open_the_circuit_and_audit_it(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="raise")
    for _ in range(health.FAILURES_TO_OPEN):
        with pytest.raises(ProviderUnavailable) as err:
            validator_call()
        assert err.value.reason == "error"
    db_session.expire_all()
    inst = db_session.get(PluginInstallation, VALIDATOR)
    assert health.circuit_open(inst)
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "circuit_open"
    assert failures(db_session, VALIDATOR) == health.FAILURES_TO_OPEN, "an open circuit is not another failure"
    entries = [e for e in audit_actions(db_session) if e.action == "plugin.provider_failed"]
    assert len(entries) == 1
    meta = entries[0].event_metadata
    assert meta["provider"] == "tax_id_validator:fixture" and meta["plugin_id"] == VALIDATOR
    assert meta["failures"] == health.FAILURES_TO_OPEN
    assert providers_of("tax_id_validator") == []


def test_the_circuit_also_stops_every_other_method(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, mode="raise")
    for _ in range(health.FAILURES_TO_OPEN):
        with pytest.raises(ProviderUnavailable):
            validator_call()
    with pytest.raises(ProviderUnavailable) as err:
        validator_call("supports", "DE", "eu_vat")
    assert err.value.reason == "circuit_open"


def test_a_disabled_or_unknown_plugin_is_unavailable(plugin_env, db_session):
    inst = setup_plugin(plugin_env, db_session, VALIDATOR)
    with pytest.raises(ProviderUnavailable) as err:
        validator_call(ref="nobody:fixture")
    assert err.value.reason == "not_installed"
    for ref, reason in ((f"{VALIDATOR}:other", "unknown_provider"), (VALIDATOR, "unknown_provider"), ("", "unknown_provider")):
        with pytest.raises(ProviderUnavailable) as err:
            validator_call(ref=ref)
        assert err.value.reason == reason
    inst.status = PluginStatus.DISABLED
    db_session.commit()
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "disabled"
    assert failures(db_session, VALIDATOR) == 0


def test_bad_calls_raise_only_provider_unavailable(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR)
    cases = [
        ("tax_id_validator", f"{VALIDATOR}:fixture", "explode", (), "unknown_method"),
        ("payment_gateway", f"{VALIDATOR}:fixture", "charge", (), "unknown_method"),
        ("tax_id_validator", f"{VALIDATOR}:fixture", "supports", ("DE",), "bad_arguments"),
        ("tax_id_validator", f"{VALIDATOR}:fixture", "check_online", (threading.Lock(),), "bad_arguments"),
        ("tax_rule_pack", f"{VALIDATOR}:fixture", "pack_id", (), "unknown_provider"),
    ]
    for kind, ref, method, args, reason in cases:
        with pytest.raises(ProviderUnavailable) as err:
            call_provider(kind, ref, method, *args)
        assert err.value.reason == reason, (method, err.value.reason)
    assert failures(db_session, VALIDATOR) == 0


def test_a_plugin_without_the_permission_is_not_called(plugin_env, db_session):
    inst = setup_plugin(plugin_env, db_session, VALIDATOR)
    inst.granted_permissions = []
    db_session.commit()
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "not_permitted"
    assert providers_of("tax_id_validator") == []


# ------------------------------------------------------------------ rate limit


def test_the_rate_limit_answers_rate_limited_without_calling_the_plugin(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, rate_limit={"calls": 3, "per_seconds": 3600})
    for _ in range(3):
        assert validator_call().status == "valid"
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "rate_limited"
    assert failures(db_session, VALIDATOR) == 0, "a refused call is not the plugin's failure"
    # the cheap methods are not metered
    assert validator_call("supports", "DE", "eu_vat") is True
    assert validator_call("check_format", "DE", "1") is True


def test_the_rate_limit_is_shared_by_two_processes_through_redis(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR, rate_limit={"calls": 3, "per_seconds": 3600})
    validator_call()
    validator_call()
    # A second process shares Redis but has no local counters and a cold plugin cache.
    common._local_counters.clear()
    runtime.reset_all()
    assert validator_call().status == "valid"
    common._local_counters.clear()
    runtime.reset_all()
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "rate_limited"
    assert not common._local_counters, "nothing was counted locally while Redis was up"


def test_the_rate_limit_falls_back_to_a_per_process_count_when_redis_is_down(plugin_env, db_session, monkeypatch):
    import app.core.redis as redis_module

    class Down:
        def incr(self, *_a, **_k):
            raise ConnectionError("redis is down")

        expire = incr

    setup_plugin(plugin_env, db_session, VALIDATOR, rate_limit={"calls": 2, "per_seconds": 3600})
    monkeypatch.setattr(redis_module, "redis_client", Down())
    common._local_counters.clear()
    validator_call()
    validator_call()
    with pytest.raises(ProviderUnavailable) as err:
        validator_call()
    assert err.value.reason == "rate_limited"
    assert common._local_counters


def test_providers_without_a_rate_limit_are_not_limited(plugin_env, db_session):
    setup_plugin(plugin_env, db_session, VALIDATOR)
    for _ in range(8):
        validator_call()


# ------------------------------------------------------------------ answer validation (units)


def part(**changes):
    values = dict(name="VAT", rate_percent="20.0000", jurisdiction="GB", base_cents=1000, tax_cents=200)
    values.update(changes)
    return kinds.TaxProviderPart(**values)


def answer(parts, key="a", **changes):
    return kinds.TaxProviderResult(lines={key: tuple(parts)}, **changes)


def validate_tax(result, keys=("a",)):
    request = tax_request(lines=tuple(kinds.TaxProviderLine(k, 100, "standard", "service", None) for k in keys))
    return kinds.validate_answer("tax_provider", "calculate", (request,), result)


@pytest.mark.parametrize("tax", [199, 200, 201])
def test_tax_within_one_minor_unit_of_the_rate_is_accepted(tax):
    assert validate_tax(answer([part(tax_cents=tax)])).lines["a"][0].tax_cents == tax


@pytest.mark.parametrize("tax", [0, 198, 202, 1000])
def test_tax_further_from_the_rate_is_refused(tax):
    with pytest.raises(kinds.InvalidAnswer, match="rate and base"):
        validate_tax(answer([part(tax_cents=tax)]))


@pytest.mark.parametrize("rate", ["20.12345", "-1", "ten", "", "1e2", " 20", "1000.0"])
def test_rates_are_plain_decimals_with_up_to_four_decimals(rate):
    with pytest.raises(kinds.InvalidAnswer, match="rate"):
        validate_tax(answer([part(rate_percent=rate)]))


def test_several_parts_per_line_and_reverse_charge():
    parts = [part(name="State", rate_percent="5.0000", tax_cents=50), part(name="City", rate_percent="1.5000", tax_cents=15)]
    assert len(validate_tax(answer(parts)).lines["a"]) == 2
    ok = part(reverse_charge=True, tax_cents=0, rate_percent="0.0000", legal_note="Reverse charge")
    assert validate_tax(answer([ok])).lines["a"][0].reverse_charge is True
    with pytest.raises(kinds.InvalidAnswer, match="reverse charge with tax"):
        validate_tax(answer([part(reverse_charge=True)]))


def test_a_list_of_parts_is_normalised_to_a_tuple():
    result = kinds.TaxProviderResult(lines={"a": [part()]})  # type: ignore[arg-type]
    assert isinstance(validate_tax(result).lines["a"], tuple)


def test_booleans_are_not_integers_and_texts_are_bounded():
    with pytest.raises(kinds.InvalidAnswer, match="not an integer"):
        validate_tax(answer([part(base_cents=True, tax_cents=0)]))
    with pytest.raises(kinds.InvalidAnswer, match="jurisdiction"):
        validate_tax(answer([part(jurisdiction="")]))
    with pytest.raises(kinds.InvalidAnswer, match="reference"):
        validate_tax(answer([part()], reference="x" * 129))
    with pytest.raises(kinds.InvalidAnswer, match="a dict by line key"):
        validate_tax(kinds.TaxProviderResult(lines=[]))  # type: ignore[arg-type]


def test_validator_results_are_normalised():
    naive = kinds.TaxIdCheckResult("valid", datetime(2026, 1, 1), registered_name="n" * 600, registered_address="a" * 600)
    out = kinds.validate_answer("tax_id_validator", "check_online", (), naive)
    assert out.checked_at.tzinfo is not None
    assert len(out.registered_name) == 512 and len(out.registered_address) == 512
    with pytest.raises(kinds.InvalidAnswer, match="request_ref"):
        kinds.validate_answer("tax_id_validator", "check_online", (), kinds.TaxIdCheckResult("valid", NOW, request_ref="r" * 65))
    with pytest.raises(kinds.InvalidAnswer, match="error_code"):
        kinds.validate_answer("tax_id_validator", "check_online", (), kinds.TaxIdCheckResult("invalid", NOW, error_code="e" * 65))
    with pytest.raises(kinds.InvalidAnswer, match="checked_at"):
        kinds.validate_answer("tax_id_validator", "check_online", (), kinds.TaxIdCheckResult("valid", "yesterday"))  # type: ignore[arg-type]
    with pytest.raises(kinds.InvalidAnswer, match="true or false"):
        kinds.validate_answer("tax_id_validator", "supports", (), "yes")


def test_pack_answers_are_checked():
    assert kinds.validate_answer("tax_rule_pack", "pack_id", (), "uk_vat") == "uk_vat"
    for bad in ("", "Bad Id", 5, "x" * 65):
        with pytest.raises(kinds.InvalidAnswer):
            kinds.validate_answer("tax_rule_pack", "pack_id", (), bad)
    assert kinds.validate_answer("tax_rule_pack", "versions", (), ["1", "2"]) == ["1", "2"]
    with pytest.raises(kinds.InvalidAnswer):
        kinds.validate_answer("tax_rule_pack", "versions", (), [1])
    with pytest.raises(kinds.InvalidAnswer):
        kinds.validate_answer("tax_rule_pack", "versions", (), "1")
    with pytest.raises(kinds.InvalidAnswer, match="not JSON"):
        kinds.validate_answer("tax_rule_pack", "load", ("1",), {"x": object()})
    with pytest.raises(kinds.InvalidAnswer, match="1 MB"):
        kinds.validate_answer("tax_rule_pack", "load", ("1",), {"x": "y" * (kinds.MAX_PACK_BYTES + 1)})
    data = {"a": [1, 2]}
    loaded = kinds.validate_answer("tax_rule_pack", "load", ("1",), data)
    assert loaded == data and loaded is not data


# ------------------------------------------------------------------ bundled discovery


@pytest.fixture
def bundled(plugin_env, monkeypatch, tmp_path):
    root = tmp_path / "bundled"
    root.mkdir(exist_ok=True)
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(root))
    return root


def test_discover_reads_both_roots_and_marks_bundled_packages(plugin_env, bundled):
    put_fixture(bundled, VALIDATOR)
    write_plugin(plugin_env, base_manifest(), "def register(plugin):\n    pass\n")
    found = {p.directory: p for p in discover()}
    assert set(found) == {VALIDATOR, "demo_plugin"}
    assert found[VALIDATOR].bundled is True and found[VALIDATOR].valid
    assert found["demo_plugin"].bundled is False
    assert [p.directory for p in discover(plugin_env)] == ["demo_plugin"], "an explicit root is read alone"
    assert find_package(VALIDATOR).bundled is True


def test_a_missing_bundled_folder_is_fine(plugin_env, monkeypatch, tmp_path):
    monkeypatch.setattr(settings, "bundled_plugins_dir", str(tmp_path / "nowhere"))
    assert discover() == []


def test_a_relative_bundled_setting_counts_from_the_repository_root(monkeypatch):
    from app.plugin_host.loader import bundled_root

    monkeypatch.setattr(settings, "bundled_plugins_dir", "plugin_packages")
    assert bundled_root() == Path(__file__).resolve().parents[2] / "plugin_packages"


def test_a_bundled_plugin_installs_runs_and_is_listed_as_bundled(plugin_env, bundled, db_session):
    put_fixture(bundled, VALIDATOR, provides={"rate_limit": None})
    overview = lifecycle.overview(db_session)
    assert overview["bundled_plugins_dir"] == str(bundled.resolve())
    available = [p for p in overview["available"] if p["directory"] == VALIDATOR]
    assert available and available[0]["bundled"] is True and available[0]["installed"] is False
    inst = install(db_session, VALIDATOR)
    assert inst.directory == VALIDATOR and inst.effective_api == "1.1"
    assert validator_call().status == "valid"
    summary = lifecycle.summary(db_session, inst, find_package(VALIDATOR))
    assert summary["bundled"] is True
    assert summary["provides"] == [
        {"kind": "tax_id_validator", "id": "fixture", "label": "Fixture validator", "ref": f"{VALIDATOR}:fixture"}
    ]
    run(lifecycle.set_enabled(db_session, inst, False))
    run(lifecycle.uninstall(db_session, inst))
    assert (bundled / VALIDATOR / "plugin.json").is_file(), "uninstall never touches the bundled folder"


def test_the_same_plugin_in_both_roots_refuses_the_plugins_folder_copy_and_the_bundled_one_runs(
    plugin_env, bundled, db_session
):
    put_fixture(bundled, VALIDATOR)
    install(db_session, VALIDATOR)
    put_fixture(plugin_env, VALIDATOR)
    (plugin_env / VALIDATOR / "notes.txt").write_text("a different copy")
    packages = {p.bundled: p for p in discover() if p.directory == VALIDATOR}
    assert packages[True].valid and not packages[True].errors
    assert not packages[False].valid and packages[False].reserved
    assert any("is a reserved name used by a plugin Rackflow ships" in e for e in packages[False].errors)
    assert find_package(VALIDATOR).bundled is True and find_package(VALIDATOR).valid
    runtime.reset_all()
    assert validator_call().status == "valid"
    db_session.expire_all()
    assert db_session.get(PluginInstallation, VALIDATOR).status == PluginStatus.ENABLED
    with pytest.raises(lifecycle.LifecycleError, match="already installed"):
        run(lifecycle.upgrade(db_session, db_session.get(PluginInstallation, VALIDATOR), accept_permissions=[]))


def test_the_same_plugin_id_under_another_folder_name_is_refused_and_the_bundled_one_is_not(plugin_env, bundled):
    put_fixture(bundled, VALIDATOR)
    shutil.copytree(bundled / VALIDATOR, plugin_env / "renamed_copy")
    packages = {(p.directory, p.bundled): p for p in discover()}
    assert set(packages) == {(VALIDATOR, True), ("renamed_copy", False)}
    assert packages[(VALIDATOR, True)].valid
    assert any("reserved name" in e for e in packages[("renamed_copy", False)].errors)
    assert find_package(VALIDATOR).valid
    assert any(f"`{VALIDATOR}` is a reserved name" in e for e in find_package("renamed_copy").errors)


def test_a_pack_file_over_1_mb_is_refused_before_it_is_read(plugin_env, monkeypatch):
    path = put_fixture(plugin_env, PACK)
    pack = path / "packs" / "demo.json"
    data = json.loads(pack.read_text())
    data["padding"] = "x" * kinds.MAX_PACK_BYTES
    pack.write_text(json.dumps(data))
    read_text = Path.read_text

    def no_big_reads(self, *args, **kwargs):
        assert self != pack, "the oversized pack was read whole"
        return read_text(self, *args, **kwargs)

    monkeypatch.setattr(Path, "read_text", no_big_reads)
    assert any("packs/demo.json is larger than 1 MB" in e for e in find_package(PACK).errors)


def test_installing_a_plugin_present_in_both_roots_is_refused(plugin_env, bundled, db_session):
    put_fixture(bundled, VALIDATOR)
    put_fixture(plugin_env, VALIDATOR)
    with pytest.raises(lifecycle.LifecycleError, match="reserved name"):
        run(lifecycle.install(db_session, VALIDATOR, accept_permissions=["tax.validate_id"]))


def test_the_plugins_folder_wins_for_a_plugin_only_there(plugin_env, bundled, db_session):
    put_fixture(plugin_env, PACK)
    inst = install(db_session, PACK)
    assert find_package(PACK).bundled is False
    assert lifecycle.summary(db_session, inst, find_package(PACK))["bundled"] is False
