"""A tax_rule_pack for the host tests: serves the JSON file shipped in the package."""

import json
from pathlib import Path

PACKS = Path(__file__).parent / "packs"


def register(plugin):
    @plugin.provider("tax_rule_pack", "demo_pack")
    class Pack:
        def pack_id(self):
            return "demo_pack"

        def versions(self):
            return ["2026.11.1"]

        def load(self, version):
            if version != "2026.11.1":
                raise KeyError(version)
            return json.loads((PACKS / "demo.json").read_text(encoding="utf-8"))
