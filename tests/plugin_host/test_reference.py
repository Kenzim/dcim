"""The plugin API reference, the API surface snapshots and the guide must not drift from the code."""

import json
import re

import pytest

from app.plugin_host import sdk
from app.plugin_host.catalogue import API_VERSION, EVENT_LIST, PERMISSION_LIST
from app.plugin_host.testkit import reference

ROOT = reference.DOCS_DIR.parents[1]
REGENERATE = "run `python -m app.plugin_host.testkit.reference --write` and commit the result"


def test_reference_matches_the_code():
    assert reference.REFERENCE_FILE.read_text(encoding="utf-8") == reference.render_reference(), (
        f"docs/plugins/api-reference.md is out of date; {REGENERATE}"
    )


def test_surface_snapshot_of_this_version_matches_the_code():
    path = reference.surface_file()
    assert path.is_file(), f"{path.name} is missing; {REGENERATE}"
    assert path.read_text(encoding="utf-8") == reference.surface_text(), f"{path.name} is out of date; {REGENERATE}"


def test_every_event_and_sdk_member_has_a_description():
    events = {e.name for e in EVENT_LIST}
    assert set(reference.EVENT_NOTES) == events, "EVENT_NOTES in the reference generator must describe every event, and only those"
    members = {e["name"] for e in reference.sdk_surface()}
    missing = sorted(members - set(reference.SDK_NOTES))
    stale = sorted(set(reference.SDK_NOTES) - members)
    assert not missing, f"describe these SDK members in SDK_NOTES: {missing}"
    assert not stale, f"SDK_NOTES names members that no longer exist: {stale}"


def test_every_provider_refusal_reason_has_a_description():
    reasons = reference.provider_reasons()
    assert "busy" in reasons and len(reasons) == len(set(reasons))
    assert set(reference.REASON_NOTES) == set(reasons), "REASON_NOTES must describe every ProviderUnavailable reason, and only those"
    assert set(reference.FAILURE_REASONS) <= set(reasons)


def test_field_notes_name_real_manifest_fields():
    known = set()
    for model in reference._MODELS:
        for name, info in model.model_fields.items():
            known.add(f"{model.__name__}.{reference._field_name(name, info)}")
    assert not sorted(set(reference.FIELD_NOTES) - known), "FIELD_NOTES names fields that do not exist"


def _surface_files():
    return sorted(reference.DOCS_DIR.glob("api-surface-*.json"))


@pytest.mark.parametrize("path", _surface_files(), ids=lambda p: p.name)
def test_api_only_grows_within_a_major_version(path):
    """Everything an earlier surface of the same major version offered must still be there."""
    old = json.loads(path.read_text(encoding="utf-8"))
    if old["api_version"].split(".")[0] != API_VERSION.split(".")[0]:
        pytest.skip("another major version")
    new = reference.api_surface()
    problems = []
    problems += [f"permission {p} was removed" for p in set(old["permissions"]) - set(new["permissions"])]
    for name, spec in old["events"].items():
        current = new["events"].get(name)
        if current is None:
            problems.append(f"event {name} was removed")
            continue
        gone = set(spec["required"] + spec["optional"]) - set(current["required"] + current["optional"])
        problems += [f"event {name} lost the field {f}" for f in sorted(gone)]
        if current["permission"] != spec["permission"]:
            problems.append(f"event {name} needs a different permission")
        if spec["before"] and not current["before"]:
            problems.append(f"event {name} lost its before hook")
    for model, spec in old["manifest"].items():
        current = new["manifest"].get(model)
        if current is None:
            problems.append(f"manifest model {model} was removed")
            continue
        problems += [f"{model} lost the field {f}" for f in sorted(set(spec["fields"]) - set(current["fields"]))]
        problems += [
            f"{model} gained the required field {f}" for f in sorted(set(current["required"]) - set(spec["required"]))
        ]
    for key in ("sdk", "lifecycle", "errors", "power_actions", "power_sources"):
        problems += [f"{key} entry {v} was removed" for v in sorted(set(old[key]) - set(new[key]))]
    assert not problems, "breaking changes within a major version:\n" + "\n".join(problems)


def test_contract_document_lists_every_event_and_permission():
    text = (ROOT / "docs" / "plugin-system.md").read_text(encoding="utf-8")
    missing = [e.name for e in EVENT_LIST if f"`{e.name}`" not in text]
    missing += [p.key for p in PERMISSION_LIST if f"`{p.key}`" not in text]
    assert not missing, f"docs/plugin-system.md does not mention: {missing}"


def _sdk_names() -> set[str]:
    names = set()
    state_methods = set()
    for entry in reference.sdk_surface():
        name = entry["name"]
        if name.startswith("state."):
            state_methods.add(name.split(".", 1)[1])
        parts = name.split(".")
        for i in range(2, len(parts) + 1):
            names.add(".".join(parts[:i]))
    names |= {f"ctx.errors.{e}" for e in vars(sdk.errors)} | {"ctx.errors", "ctx.ActionError"}
    names |= {f"ctx.store.{m}" for m in state_methods}
    return {n for n in names if n.startswith("ctx.")}


def _documents():
    files = sorted(reference.DOCS_DIR.glob("*.md"))
    files += sorted((ROOT / "examples" / "plugins").glob("*/*.py"))
    files += sorted((ROOT / "examples" / "plugins").glob("*/README.md"))
    return files


@pytest.mark.parametrize("path", _documents(), ids=lambda p: str(p.relative_to(ROOT)))
def test_documents_only_use_sdk_members_that_exist(path):
    names = _sdk_names()
    text = path.read_text(encoding="utf-8")
    used = set(re.findall(r"\bctx\.[A-Za-z_]+(?:\.[A-Za-z_]+)?", text))
    unknown = sorted(u for u in used if u not in names)
    assert not unknown, f"{path.name} uses SDK members that do not exist: {unknown}"


def test_guide_links_resolve():
    broken = []
    for path in sorted(reference.DOCS_DIR.glob("*.md")):
        for target in re.findall(r"\]\(([^)#\s]+)(?:#[^)]*)?\)", path.read_text(encoding="utf-8")):
            if target.startswith(("http://", "https://", "mailto:")):
                continue
            if not (path.parent / target).resolve().exists():
                broken.append(f"{path.name} -> {target}")
    assert not broken, f"broken links: {broken}"
