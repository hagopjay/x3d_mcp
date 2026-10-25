"""In-process callers (playground/report.py) call the validators with a scene string.

The signatures and the default results of those functions must not change when the
transfer layer and the JSON encoding were added: only optional, appended parameters.
The strings below are the signatures at the commit before that work (8dd0d3e).
"""

import inspect
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from validation import repair, semantic, validate
from x3d_utils import source

BASE_SIGNATURES = [
    (validate, "validate_xml", "(xml_string: str, version: str | None = None) -> dict"),
    (validate, "validate_json", "(json_string: str) -> dict"),
    (validate, "validate_scene", "(scene_manager) -> dict"),
    (validate, "resolve_xsd_version", "(declared: str | None) -> tuple[str, str | None]"),
    (semantic, "semantic_diagnostics", "(xml_string: str) -> list[validation.semantic.Diagnostic]"),
    (semantic, "validate_semantic", "(xml_string: str) -> str"),
    (repair, "repair_x3d", "(xml_string: str) -> dict"),
]

SCENE = ('<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="Interchange" version="4.0"><head/><Scene>'
         '<Viewpoint DEF="V" position="0 0 5"/><Shape><Appearance><Material DEF="M"/></Appearance>'
         '<Box/></Shape></Scene></X3D>')


@pytest.mark.parametrize("module, name, expected", BASE_SIGNATURES, ids=[b[1] for b in BASE_SIGNATURES])
def test_signature_of_a_function_the_playground_calls_is_unchanged(module, name, expected):
    assert str(inspect.signature(getattr(module, name))) == expected


def test_the_playground_imports_exactly_these_functions():
    """If report.py starts using another validator, this list (and the one above) must grow with it."""
    text = (Path(__file__).resolve().parent.parent / "playground" / "report.py").read_text()
    used = sorted(line.strip() for line in text.splitlines() if line.startswith(("from validation", "from x3d_utils")))
    assert used == ["from validation.repair import repair_x3d",
                    "from validation.semantic import NoSceneError, semantic_diagnostics",
                    "from validation.validate import validate_xml"]


def test_load_x3d_source_only_gained_an_optional_trailing_parameter():
    params = list(inspect.signature(source.load_x3d_source).parameters.values())
    assert [p.name for p in params] == ["content", "path", "blob_id"]
    assert all(p.default is None for p in params)
    assert source.load_x3d_source("<X3D/>") == "<X3D/>" and source.load_x3d_source(path=None, content="x") == "x"


def test_default_results_are_what_they_were():
    result = validate.validate_xml(SCENE)
    assert result["valid"] is True and list(result) == ["valid", "errors", "schema_version"]
    assert result == validate.validate_xml(SCENE, None)
    assert isinstance(semantic.semantic_diagnostics(SCENE), list)
    assert isinstance(semantic.validate_semantic(SCENE), str)
    fixed = repair.repair_x3d(SCENE)
    assert list(fixed) == ["fixed", "changes", "unfixable", "remaining"]
    assert validate.validate_json("{}")["valid"] is False


def test_the_json_schema_fix_changes_validate_json_for_navigationinfo_only():
    """validate_json's result for a document does not depend on the fix unless it holds NavigationInfo.@type."""
    doc = ('{"X3D": {"encoding": "UTF-8", "@profile": "Immersive", "@version": "4.0", "Scene": {"-children": '
           '[{"Group": {}}, {"Viewpoint": {"@position": [0, 0, 5]}}]}}}')
    assert validate.validate_json(doc)["valid"] is True
    nav = doc.replace('{"Group": {}}', '{"NavigationInfo": {"@type": ["WALK", "ANY"]}}')
    assert validate.validate_json(nav)["valid"] is True
    assert validate.validate_json(nav.replace('["WALK", "ANY"]', '"WALK"'))["valid"] is False   # a bare string was the old, wrong form
