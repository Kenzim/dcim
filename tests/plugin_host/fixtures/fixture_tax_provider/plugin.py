"""A tax_provider for the host tests: 10 per cent on every line unless `mode` says otherwise."""

from rackflow_plugin.kinds import TaxProviderPart, TaxProviderResult, current_context


def _part(line, **changes):
    values = dict(
        name="Flat 10%",
        rate_percent="10.0000",
        jurisdiction="XX",
        base_cents=line.net_cents,
        tax_cents=line.net_cents // 10,
    )
    values.update(changes)
    return TaxProviderPart(**values)


def register(plugin):
    @plugin.provider("tax_provider", "flat")
    class Flat:
        def supports(self, request):
            return request.currency in ("EUR", "GBP") and request.buyer.get("country") == "XX"

        def calculate(self, request):
            mode = current_context().settings.get("mode")
            if mode == "raise":
                raise ValueError("no tax today")
            lines = {line.key: (_part(line),) for line in request.lines}
            if mode == "missing_key":
                lines.pop(request.lines[0].key)
            elif mode == "extra_key":
                lines["ghost"] = (_part(request.lines[0]),)
            elif mode == "negative":
                lines[request.lines[0].key] = (_part(request.lines[0], tax_cents=-1),)
            elif mode == "wrong_tax":
                lines[request.lines[0].key] = (_part(request.lines[0], tax_cents=request.lines[0].net_cents),)
            elif mode == "reverse_charge_with_tax":
                lines[request.lines[0].key] = (_part(request.lines[0], reverse_charge=True),)
            elif mode == "float_amount":
                lines[request.lines[0].key] = (_part(request.lines[0], tax_cents=1.5),)
            elif mode == "bad_rate":
                lines[request.lines[0].key] = (_part(request.lines[0], rate_percent="ten"),)
            elif mode == "mutate_request":
                request.buyer["country"] = "ZZ"
            return TaxProviderResult(lines=lines, reference="TX-1")
