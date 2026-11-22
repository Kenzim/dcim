"""The email template sandbox: it renders plain data and refuses everything else (design 12.1)."""

from __future__ import annotations

import time
from datetime import date, datetime
from decimal import Decimal

import pytest

from app.services.email_templates import sandbox
from app.services.email_templates.sandbox import EmailTemplateError, compile_source, render_source, sanitise_variables

DATA = {
    "name": "Alex",
    "amount_cents": 1234,
    "currency": "gbp",
    "items": ["a", "b", "c"],
    "lines": [{"label": "VM", "cents": 500}, {"label": "IP", "cents": 100}],
    "url": 'https://example.com/?a=1&b="2"',
    "when": "2026-12-01",
}


def text(source, data=None):
    return render_source(source, DATA if data is None else data)


def html(source, data=None):
    return render_source(source, DATA if data is None else data, html=True)


def refused(source, kind, data=None, *, as_html=False):
    with pytest.raises(EmailTemplateError) as info:
        render_source(source, DATA if data is None else data, html=as_html)
    assert info.value.kind == kind, (source, info.value.message)
    return info.value


def test_plain_rendering_and_filters():
    assert text("Hello {{ name }}!") == "Hello Alex!"
    assert text("{{ amount_cents|money(currency) }}") == "GBP 12.34"
    assert text("{{ 5|money('usd') }} {{ -5|money('usd') }} {{ 100000|money('eur') }}") == "USD 0.05 USD -0.05 EUR 1000.00"
    assert text("{{ when|date('%d %B %Y') }}") == "01 December 2026"
    assert text("{{ items|join(', ') }}|{{ items|length }}|{{ name|upper }}|{{ missing|default('x') }}") == "a, b, c|3|ALEX|x"
    assert text("{% for line in lines %}{{ loop.index }}. {{ line.label }} {{ line.cents|money('GBP') }}\n{% endfor %}") == (
        "1. VM GBP 5.00\n2. IP GBP 1.00\n"
    )
    assert text("{% if amount_cents > 1000 %}big{% elif amount_cents > 10 %}mid{% else %}small{% endif %}") == "big"
    assert text("{{ 'a' ~ name ~ 'b' }} {{ items[1] }} {{ lines[0]['label'] }} {{ items|first }}{{ items|last }}") == "aAlexb b VM ac"


def test_money_matches_the_built_in_formatting():
    from app.services.email_templates.catalogue import money_text

    for cents in (0, 1, 9, 99, 100, 1234, 99999999, 123456789012):
        for currency in ("USD", "gbp", None, ""):
            assert sandbox.money_filter(cents, currency) == money_text({"amount_cents": cents, "currency": currency})


def test_html_environment_escapes_variables_but_not_the_template():
    assert html("<a href=\"{{ url }}\">{{ name }}</a>") == '<a href="https://example.com/?a=1&amp;b=&#34;2&#34;">Alex</a>'
    assert html("{{ '<b>' }}") == "&lt;b&gt;"
    assert text("{{ '<b>' }}") == "<b>"


@pytest.mark.parametrize(
    "source",
    [
        "{{ name.__class__ }}",
        "{{ name.__class__.__mro__ }}",
        "{{ name.__class__.mro() }}",
        "{{ ''.__class__.__mro__[1].__subclasses__() }}",
        "{{ name['__class__'] }}",
        "{{ items.__globals__ }}",
        "{{ lines[0].__init__.__globals__ }}",
        "{{ loop.__class__ }}",
        "{% for x in items %}{{ x.__class__ }}{% endfor %}",
        "{{ self }}",
        "{{ self.__init__ }}",
        "{{ _ }}",
        "{{ __builtins__ }}",
        "{{ name|attr('__class__') }}",
        "{{ name|map('upper') }}",
        "{{ cycler }}",
        "{{ range(10) }}",
        "{{ lipsum() }}",
        "{{ namespace() }}",
        "{{ dict(a=1) }}",
        "{{ config }}",
        "{{ request }}",
        "{{ get_flashed_messages }}",
    ],
)
def test_escape_attempts_never_render(source):
    with pytest.raises(EmailTemplateError):
        render_source(source, DATA)
    with pytest.raises(EmailTemplateError):
        render_source(source, DATA, html=True)


def test_internal_attributes_are_named_in_the_error():
    error = refused("{{ name.__class__ }}", "security")
    assert "__class__" in error.message
    assert error.line == 1


def test_attribute_access_on_values_is_refused_even_when_public():
    refused("{{ name.upper }}", "security")
    refused("{{ items.append }}", "security")
    refused("{{ lines.sort }}", "security")
    assert text("{{ lines[0].items }}", {"lines": [{"items": 3}]}) == "3"


def test_calls_are_refused():
    refused("{{ name.upper() }}", "security")
    refused("{{ money(1, 'GBP') }}", "security")
    refused("{{ (name.upper)() }}", "security")
    refused("{% for x in items %}{{ loop.cycle('a', 'b') }}{% endfor %}", "security")


@pytest.mark.parametrize(
    ("source", "word"),
    [
        ("{% include 'x' %}", "include"),
        ("{% import 'x' as y %}", "import"),
        ("{% from 'x' import y %}", "import"),
        ("{% extends 'x' %}", "extends"),
        ("{% block a %}{% endblock %}", "block"),
        ("{% macro m() %}x{% endmacro %}", "macro"),
        ("{% set a = 1 %}{{ a }}", "set"),
        ("{% set a %}x{% endset %}", "set"),
        ("{% with a = 1 %}{{ a }}{% endwith %}", "with"),
        ("{% filter upper %}x{% endfilter %}", "filter"),
        ("{% autoescape false %}{{ name }}{% endautoescape %}", "autoescape"),
        ("{{ 'a' * 100 }}", "*"),
        ("{{ 2 ** 64 }}", "**"),
        ("{{ '%s' % name }}", "%"),
        ("{{ {'a': 1} }}", "dictionary"),
    ],
)
def test_forbidden_syntax_is_refused_at_compile_time(source, word):
    with pytest.raises(EmailTemplateError) as info:
        compile_source(source)
    assert info.value.kind == "security"
    assert word in info.value.message


def test_include_cannot_read_files_even_when_the_loader_is_asked():
    refused("{% include '/etc/passwd' %}", "security")
    refused("{% include 'app/main.py' %}", "security")


def test_nested_loops_and_recursive_loops_are_refused():
    refused("{% for a in items %}{% for b in items %}x{% endfor %}{% endfor %}", "security")
    refused("{% for a in items recursive %}x{% endfor %}", "security")
    assert text("{% for a in items %}{{ a }}{% endfor %}{% for b in items %}{{ b }}{% endfor %}") == "abcabc"


def test_unknown_filters_and_unsafe_filters_are_refused():
    for name in ("format", "indent", "center", "wordwrap", "attr", "map", "safe", "tojson", "random", "xmlattr", "filesizeformat"):
        with pytest.raises(EmailTemplateError):
            compile_source("{{ name|%s }}" % name)
    with pytest.raises(EmailTemplateError):
        compile_source("{{ name is callable }}")


def test_strict_undefined_fails_with_line_and_name():
    error = refused("line one\n{{ nope }}", "undefined")
    assert "nope" in error.message
    assert error.line == 2
    refused("{{ lines[0].missing }}", "undefined")
    refused("{{ lines[5] }}", "undefined")
    refused("{% if nope %}x{% endif %}", "undefined")
    assert text("{{ nope|default('ok') }}") == "ok"


def test_syntax_errors_carry_the_line():
    error = refused("a\nb\n{% if %}", "syntax")
    assert error.line == 3
    refused("{{ name", "syntax")


def test_oversized_templates_are_refused_before_parsing():
    big = "x" * (sandbox.MAX_SOURCE_BYTES + 1)
    refused(big, "limit")
    assert len(text("x" * sandbox.MAX_SOURCE_BYTES)) == sandbox.MAX_SOURCE_BYTES
    multibyte = "é" * (sandbox.MAX_SOURCE_BYTES // 2 + 1)
    refused(multibyte, "limit")


def test_output_size_is_capped():
    chunk = "y" * 5000
    source = "{% for a in items %}" + "{{ a }}" * 1 + "{% endfor %}"
    assert text(source) == "abc"
    big_items = {"items": ["z" * 4000] * 100}
    template = "{% for a in items %}" + ("{{ a }}" * 4) + "{% endfor %}"
    error = refused(template, "limit", big_items)
    assert "larger" in error.message
    assert chunk


def test_too_many_elements_is_refused():
    refused("{{ name }}" * (sandbox.MAX_NODES + 1), "limit")


def test_deeply_nested_expressions_do_not_crash_the_parser():
    with pytest.raises(EmailTemplateError):
        compile_source("{{ " + "(" * 20000 + "name" + ")" * 20000 + " }}")
    with pytest.raises(EmailTemplateError):
        compile_source("{% if x %}" * 3000 + "{% endif %}" * 3000)


def test_filter_chains_cannot_blow_up_a_value():
    source = "{{ name" + ("|replace('A', 'AA" + "A" * 1000 + "')") * 40 + " }}"
    error = refused(source, "limit", {"name": "A" * 4000})
    assert "too large" in error.message
    refused("{{ name|replace('', '" + "x" * 5000 + "') }}", "limit", {"name": "A" * 4000})
    refused("{{ name|replace('A', 'ab', 1000000000) }}|{{ name|replace('A', '" + "z" * 70000 + "') }}", "limit", {"name": "A" * 4000})


def test_loops_over_data_are_bounded_by_the_data_limits():
    with pytest.raises(EmailTemplateError):
        sanitise_variables({"items": list(range(101))})
    with pytest.raises(EmailTemplateError):
        sanitise_variables({"s": "x" * (sandbox.MAX_VARIABLE_STRING + 1)})
    with pytest.raises(EmailTemplateError):
        sanitise_variables({"a": {"b": {"c": {"d": {"e": 1}}}}})
    started = time.monotonic()
    data = sanitise_variables({"items": ["x" * 4096] * 100})
    text("{% for a in items %}{% if a|length > 5 %}{{ a|length }}{% endif %}{% endfor %}", data)
    assert time.monotonic() - started < sandbox.MAX_RENDER_SECONDS


def test_the_render_deadline_stops_a_slow_template(monkeypatch):
    monkeypatch.setattr(sandbox, "MAX_RENDER_SECONDS", -1.0)
    error = refused("{{ name }}", "limit")
    assert "too long" in error.message


def test_variables_must_be_plain_data():
    class Row:
        id = 1

    with pytest.raises(EmailTemplateError):
        sanitise_variables({"row": Row()})
    with pytest.raises(EmailTemplateError):
        sanitise_variables({"call": lambda: 1})
    with pytest.raises(EmailTemplateError):
        sanitise_variables({"d": Decimal("1.5")})
    clean = sanitise_variables(
        {"when": datetime(2026, 1, 2, 3, 4), "day": date(2026, 1, 2), "n": None, "ok": True, "lines": ({"a": 1},)}
    )
    assert clean == {"when": "2026-01-02T03:04:00", "day": "2026-01-02", "n": None, "ok": True, "lines": [{"a": 1}]}


def test_date_filter_accepts_iso_strings_and_rejects_other_values():
    assert text("{{ '2026-12-01T10:30:00'|date('%H:%M') }}") == "10:30"
    refused("{{ 5|date }}", "runtime")
    refused("{{ 'not a date'|date }}", "runtime")


def test_template_cannot_see_its_own_environment():
    refused("{{ self._TemplateReference__context }}", "security")
    refused("{{ loop }}", "undefined")
    assert text("{% for a in items %}{{ loop.first }}{{ loop.last }}{{ loop.length }}{% endfor %}") == (
        "TrueFalse3FalseFalse3FalseTrue3"
    )


@pytest.mark.parametrize(
    "source",
    [
        "{% set f = 'x'.format %}{{ f(name) }}",
        "{% set f = name.format %}{{ f }}",
        "{{ name.format }}",
        "{{ name.format(name.__class__) }}",
        "{{ '{0.__class__}'.format(name) }}",
        "{{ '{0.__class__}'|format(name) }}",
        "{% for f in [name.format] %}{{ f('{0.__class__}') }}{% endfor %}",
        "{{ name|attr('format') }}",
        "{{ name|attr('__class__') }}",
        "{{ name|attr('format')('{0.__class__}') }}",
        "{{ name|xmlattr }}",
        "{{ {'a': name}|xmlattr }}",
        "{{ name|map(attribute='__class__')|list }}",
        "{{ items|map('format') }}",
    ],
)
def test_format_and_attr_payloads_of_published_sandbox_escapes_are_refused(source):
    """CVE-2024-56326, CVE-2025-27516, CVE-2024-22195 and CVE-2024-34064 need a call, a set, attr, xmlattr or format."""
    with pytest.raises(EmailTemplateError):
        render_source(source, DATA)
    with pytest.raises(EmailTemplateError):
        render_source(source, DATA, html=True)


def test_attr_and_xmlattr_are_not_filters_of_the_sandbox():
    for name in ("attr", "xmlattr", "format", "map", "select", "selectattr", "reject", "rejectattr", "groupby", "sum", "tojson"):
        assert name not in sandbox.ALLOWED_FILTERS
        assert name not in sandbox.TEXT_ENVIRONMENT.filters
        assert name not in sandbox.HTML_ENVIRONMENT.filters


LONG = "v" * 4000


def test_a_silent_loop_over_a_huge_iterable_is_refused_quickly():
    """The review repro: ~244,000 iterations of a 200-term comparison, printing nothing and calling no filter."""
    source = "{%- for c in (v|list|join(v[:60])) -%}{%- if (" + " ~ ".join(["v"] * 200) + ") == c -%}{%- endif -%}{%- endfor -%}"
    started = time.monotonic()
    error = refused(source, "limit", {"v": LONG})
    assert "loops more than" in error.message
    assert error.line == 1
    assert time.monotonic() - started < sandbox.MAX_RENDER_SECONDS + 0.5


def test_the_iteration_budget_is_shared_by_every_loop_of_a_render():
    data = {"s": "x" * 600}
    one = "{% for c in s %}{% endfor %}"
    assert text(one, data) == ""
    refused(one + one, "limit", data)
    half = {"s": "x" * 400}
    assert text(one + one, half) == ""
    assert text(one, data) == ""  # a new render starts with a new budget


def test_iterations_are_counted_for_every_kind_of_iterable():
    refused("{% for c in s %}{% endfor %}", "limit", {"s": "x" * 1001})
    refused("{% for k in d %}{% endfor %}", "limit", {"d": {str(i): i for i in range(1001)}})
    refused("{% for c in s|reverse %}{% endfor %}", "limit", {"s": ["x"] * 1001})
    refused("{% for c in s if c %}x{% else %}y{% endfor %}", "limit", {"s": ["x"] * 1001})
    refused("{% for c in '" + "a" * 2000 + "' %}{% endfor %}", "limit")
    assert text("{% for c in s %}{{ loop.index }}{% endfor %}", {"s": "x" * 1000}).endswith("1000")


def test_loops_still_work_inside_the_budget():
    assert text("{% for a in items if a != 'b' %}{{ a }}{% else %}none{% endfor %}") == "ac"
    assert text("{% for a in items|reverse %}{{ a }}{{ loop.revindex }}{% endfor %}") == "c3b2a1"
    assert text("{% for a in [] %}x{% else %}empty{% endfor %}") == "empty"


def test_the_deadline_is_checked_on_loop_iterations_and_in_operators(monkeypatch):
    monkeypatch.setattr(sandbox, "MAX_RENDER_SECONDS", -1.0)
    for source in (
        "{% for a in items %}{% endfor %}",
        "{% if (name ~ name) == name %}{% endif %}",
        "{% if (amount_cents + 1) == 3 %}{% endif %}",
        "{% if name ~ name %}{% endif %}",
    ):
        error = refused(source, "limit")
        assert "too long" in error.message, source


def test_the_guard_filters_are_not_available_to_templates():
    for name in (sandbox.GUARD_ITERATE, sandbox.GUARD_CONCAT, sandbox.GUARD_ADD):
        refused("{{ name|%s }}" % name, "security")
        assert name not in sandbox.ALLOWED_FILTERS
    refused("{{ name is nosuchtest }}", "security")


def test_concatenation_and_addition_keep_their_results():
    assert text("{{ 'a' ~ 1 ~ name }}|{{ 1 + 2 }}|{{ items + ['d'] }}|{{ 'a' + 'b' }}") == "a1Alex|3|['a', 'b', 'c', 'd']|ab"
    assert html("{{ '<' ~ name }}") == "&lt;Alex"
    assert html("{{ url ~ '<b>' }}").endswith("&lt;b&gt;")
