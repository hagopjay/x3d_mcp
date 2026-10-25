"""The functions other code calls in process keep their signatures.

The Accountability Playground (playground/report.py) imports validate_xml, semantic_diagnostics and
repair_x3d and calls them with the scene string; the tools call validate_json, validate_scene,
validate_semantic and autofix_containerfields the same way. The splat work and the transfer layer may append
optional parameters to these functions, never reorder, rename or require anything, so a caller written
against 8dd0d3e keeps working. Each entry is (name, kind, default) as the source had it at 8dd0d3e.
"""

import importlib
import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

P = inspect.Parameter
REQUIRED = P.empty

BASELINE = {
    ("validation.validate", "validate_xml"): [("xml_string", P.POSITIONAL_OR_KEYWORD, REQUIRED),
                                              ("version", P.POSITIONAL_OR_KEYWORD, None)],
    ("validation.validate", "validate_json"): [("json_string", P.POSITIONAL_OR_KEYWORD, REQUIRED)],
    ("validation.validate", "validate_scene"): [("scene_manager", P.POSITIONAL_OR_KEYWORD, REQUIRED)],
    ("validation.validate", "resolve_xsd_version"): [("declared", P.POSITIONAL_OR_KEYWORD, REQUIRED)],
    ("validation.semantic", "semantic_diagnostics"): [("xml_string", P.POSITIONAL_OR_KEYWORD, REQUIRED)],
    ("validation.semantic", "validate_semantic"): [("xml_string", P.POSITIONAL_OR_KEYWORD, REQUIRED)],
    ("validation.repair", "repair_x3d"): [("xml_string", P.POSITIONAL_OR_KEYWORD, REQUIRED)],
    ("validation.autofix", "autofix_containerfields"): [("xml_string", P.POSITIONAL_OR_KEYWORD, REQUIRED)],
}


@pytest.mark.parametrize("module,name", sorted(BASELINE))
def test_signature_starts_with_the_baseline_and_only_appends_optional_parameters(module, name):
    fn = getattr(importlib.import_module(module), name)
    params = list(inspect.signature(fn).parameters.values())
    base = BASELINE[(module, name)]
    assert [(p.name, p.kind, p.default) for p in params[:len(base)]] == base
    for extra in params[len(base):]:
        assert extra.default is not REQUIRED and extra.kind in (P.POSITIONAL_OR_KEYWORD, P.KEYWORD_ONLY), \
            f"{name}: new parameter {extra.name} must be optional"


def test_the_playground_still_imports_what_it_needs():
    for module, names in (("validation.validate", ["validate_xml"]), ("validation.semantic", ["NoSceneError", "semantic_diagnostics"]),
                          ("validation.repair", ["repair_x3d"])):
        mod = importlib.import_module(module)
        for name in names:
            assert hasattr(mod, name), f"{module}.{name}"


GOOD = '<?xml version="1.0" encoding="UTF-8"?>\n<X3D version="4.1" profile="Interchange"><Scene><Shape><Box/></Shape></Scene></X3D>'
BAD = '<?xml version="1.0" encoding="UTF-8"?>\n<X3D version="4.1" profile="Interchange"><Scene><NoSuchNode/></Scene></X3D>'


def test_ordinary_results_keep_their_shape():
    from validation.validate import validate_xml
    assert validate_xml(GOOD) == {"valid": True, "errors": [], "schema_version": "4.1"}
    bad = validate_xml(BAD)
    assert bad["valid"] is False and set(bad) <= {"valid", "errors", "schema_version", "notes"} and "NoSuchNode" in bad["errors"][0]
    broken = validate_xml("<X3D><Scene></X3D>")
    assert set(broken) == {"valid", "errors"} and broken["errors"][0].startswith("XML parse error: ")
    assert validate_xml(GOOD, "4.0")["schema_version"] == "4.0"            # `version` still overrides the document


def test_repair_still_reads_valid_from_validate_xml():
    from validation.repair import repair_x3d
    out = repair_x3d(GOOD.replace('profile="Interchange"', ""))
    assert set(out) >= {"fixed", "changes", "unfixable", "remaining"} and out["remaining"]["schema_valid"] is True
