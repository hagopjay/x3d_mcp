"""Standards disagreements: fixtures where the bundled XSD and the normative text (and X3DUOM) part ways.

Each case pins the stock-XSD verdict and the semantic verdict SEPARATELY (external review, 18 Nov 2026, defect 8:
passing one schema snapshot is an incomplete proxy for conformance). When upstream fixes the schema, the XSD pin
here goes red on purpose: update expected.json together with the schema, never one without the other.
"""
import json
from pathlib import Path

import pytest

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from validation.semantic import semantic_diagnostics  # noqa: E402
from validation.validate import validate_xml  # noqa: E402

ROOT = Path(__file__).parent / "fixtures" / "standards-disagreements"
CASES = [(d, name, want) for d in sorted(ROOT.iterdir()) if (d / "expected.json").is_file()
         for name, want in json.loads((d / "expected.json").read_text())["cases"].items()]


@pytest.mark.parametrize("folder,name,want", CASES, ids=[f"{d.name}/{n}" for d, n, _ in CASES])
def test_stock_xsd_and_semantic_verdicts_are_pinned_separately(folder, name, want):
    text = (folder / name).read_text(encoding="utf-8")
    assert validate_xml(text)["valid"] is (want["xsd_stock"] == "valid")
    errors = [d for d in semantic_diagnostics(text) if d.level == "error"]
    assert len(errors) == want["semantic_errors"]


def test_every_disagreement_names_its_clause_and_schema_copies():
    for d in sorted(ROOT.iterdir()):
        e = json.loads((d / "expected.json").read_text())
        assert e["normative"]["document"] and e["normative"]["text"]
        assert e["xsd_checked"] and all(x.get("sha256_prefix") for x in e["xsd_checked"])
