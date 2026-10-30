"""validate_xml when libxml2's XSD pattern engine gives up on a normal-size document (validation.listvalues)."""

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation import listvalues  # noqa: E402
from validation.hints import repair_with_hints  # noqa: E402
from validation.validate import validate_xml, validate_xml_with_hints  # noqa: E402


def _points(n: int, colours: list[str], extra: str = "") -> str:
    pts = " ".join(["0 0 0"] * n)
    return ("<?xml version='1.0' encoding='UTF-8'?>\n<X3D profile='Interchange' version='4.0'><Scene>\n"
            f"<Shape><PointSet><Color color='{' '.join(colours)}'/><Coordinate point='{pts}'/></PointSet></Shape>\n"
            f"{extra}</Scene></X3D>")


def _colours(n: int, bad_at: int | None) -> list[str]:
    out = ["0.5 0.5 0.5"] * n
    if bad_at is not None:
        out[bad_at] = "1.0059 1.0511 0.9"
    return out


def test_a_short_list_still_gets_the_xsd_pattern_error():
    r = validate_xml(_points(20, _colours(20, 3)))
    assert r["valid"] is False and "[facet 'pattern']" in r["errors"][0]


def test_a_deep_bad_colour_names_the_entry_not_a_size_limit():
    r = validate_xml(_points(40, _colours(40, 30)))
    assert r["valid"] is False and r.get("checked", True) is True
    assert "[value] value 31 of 40 is '1.0059 1.0511 0.9'" in r["errors"][0]
    assert not any("size" in e for e in r["errors"])
    assert any("not a size limit" in n for n in r["notes"])


def test_the_rest_of_the_document_is_still_checked():
    r = validate_xml(_points(40, _colours(40, 30), extra="<Transform translation='0 0'/>"))
    assert len(r["errors"]) == 2 and "Transform" in r["errors"][1]


def test_a_good_long_list_is_valid():
    assert validate_xml(_points(200, _colours(200, None)))["valid"] is True


def test_the_hint_clamps_and_the_repair_loop_fixes_it():
    doc = _points(40, _colours(40, 30))
    hint = validate_xml_with_hints(doc)["hints"][0]
    assert hint["rule"] == "colour-out-of-range" and hint["suggested_fix"]["op"] == "set_attribute"
    assert repair_with_hints(doc)["valid"] is True


@pytest.mark.parametrize("value,ftype,expect", [
    ("0.5 0.5 0.5 1.2 0 0", "MFColor", "value 2 of 2 is '1.2 0 0'"),
    ("0 0 0, 1 1 1", "MFVec3f", None),
    ("0 0, 0 1 1 1", "MFVec3f", "a comma inside value 1"),
    ("0 0 0 1 1", "MFVec3f", "5 numbers is not a whole number"),
    ("0 0 x", "SFVec3f", "'x' is not a valid number"),
    ("1 0", "SFColor", "SFColor takes 3 numbers, not 2"),
    ("0 1 2.5", "MFInt32", "'2.5' is not a valid number"),
    ("true false TRUE", "MFBool", "'TRUE' is not a valid boolean"),
    ('"a" "b"', "MFString", None),
])
def test_listvalues_problem(value, ftype, expect):
    got = listvalues.problem(value, ftype)
    assert (got is None) if expect is None else (expect in got)
