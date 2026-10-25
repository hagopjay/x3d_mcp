"""validate_xml on documents with a start tag over libxml2's 10,000,000-byte limit (validation.oversize)."""

import json
import sys
from pathlib import Path

import pytest
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation import oversize
from validation.validate import validate_xml

SPLAT = dict(positions="1.000000 2.000000 3.000000", scales="0.010000 0.010000 0.010000",
             orientations="0.000000 0.000000 0.000000 1.000000", opacities="0.500000",
             sphericalHarmonicsDegree0Coef0="0.500000 0.500000 0.500000")


def splat_doc(n: int, *, drop: str = "", extra_scene: str = "", wrong_count: str = "", prolog: str = "") -> str:
    """An X3D 4.1 document with one GaussianSplats node of `n` identical splats (about 130 bytes each)."""
    attrs = []
    for k, v in SPLAT.items():
        count = n - 1 if k == wrong_count else n
        if k != drop:
            attrs.append(f'{k}="{" ".join([v] * count)}"')
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n{prolog}<X3D version="4.1" profile="Immersive">\n<head>'
            f'<component name="GaussianSplats" level="1"/></head>\n<Scene>\n<GaussianSplats {" ".join(attrs)}/>\n{extra_scene}</Scene>\n</X3D>\n')


@pytest.fixture(scope="module")
def big():
    return splat_doc(90_000)            # a start tag of about 11.7 MB


def test_the_fixture_really_is_over_the_limit(big):
    with pytest.raises(etree.XMLSyntaxError) as e:
        etree.fromstring(big.encode())
    assert oversize.is_resource_limit(e.value)
    assert max(t.size for t in oversize.find_big_start_tags(big)) > oversize.START_TAG_LIMIT


def test_oversize_gets_a_verdict_not_a_parse_error(big, monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    r = validate_xml(big)
    assert r["valid"] is False and r["checked"] is False and r["verdict"] == "not-checkable-at-this-size"
    assert not any("XML parse error" in e for e in r["errors"])
    assert "Not checkable at this size" in r["errors"][0] and "GaussianSplats" in r["errors"][0]
    assert "not a well-formedness error" in r["errors"][0]
    assert r["schema_version"] == "4.1"
    json.dumps(r)                                   # validate_x3d returns this as JSON


def test_oversize_report_has_counts_and_array_check(big, monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    tag = validate_xml(big)["oversize"]["tags"][0]
    assert tag["element"] == "GaussianSplats" and tag["splats"] == 90_000 and tag["start_tag_bytes"] > 10_000_000
    assert tag["attributes"]["positions"]["numbers"] == 270_000 and tag["attributes"]["orientations"]["numbers"] == 360_000
    assert tag["array_check"]["errors"] == []


def test_oversize_structure_check_runs_on_the_rest_of_the_document(big, monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    ok = validate_xml(big)["oversize"]["structure_check"]
    assert ok["ran"] is True and ok["valid"] is True
    bad = validate_xml(splat_doc(90_000, extra_scene="<Shape><NoSuchNode/></Shape>\n"))["oversize"]["structure_check"]
    assert bad["ran"] is True and bad["valid"] is False and "NoSuchNode" in " ".join(bad["errors"])


def test_oversize_array_check_reports_a_count_mismatch(monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    r = validate_xml(splat_doc(90_000, wrong_count="scales"))
    errs = r["oversize"]["tags"][0]["array_check"]["errors"]
    assert errs and "scales" in errs[0]
    assert errs[0] in r["errors"]                   # surfaced at the top level too


def test_oversize_missing_base_colour_is_a_warning_like_the_semantic_check(monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    r = validate_xml(splat_doc(115_000, drop="sphericalHarmonicsDegree0Coef0"))
    check = r["oversize"]["tags"][0]["array_check"]
    assert check["errors"] == [] and any("no base colour" in w for w in check["warnings"])
    assert r["valid"] is False and r["verdict"] == "not-checkable-at-this-size"      # the verdict is about size, not the warning


def with_bad_token(doc: str) -> str:
    """`doc` with the first orientations number replaced by a word."""
    head, rest = doc.split('orientations="', 1)
    return head + 'orientations="abc' + rest[len("0.000000"):]


def test_oversize_non_numeric_token_is_an_array_syntax_error(monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    errs = validate_xml(with_bad_token(splat_doc(90_000)))["oversize"]["tags"][0]["array_check"]["errors"]
    assert errs == ["orientations: not a number: 'abc'."]


def test_oversize_unknown_attribute_is_left_to_the_xsd(monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    doc = splat_doc(90_000).replace("<GaussianSplats ", f'<GaussianSplats bogus="{"1 " * 40000}" ', 1)
    sc = validate_xml(doc)["oversize"]["structure_check"]
    assert sc["ran"] is True and sc["valid"] is False and "bogus" in " ".join(sc["errors"])


def test_malformed_xml_still_says_parse_error():
    r = validate_xml("<X3D><Scene></X3D>")
    assert r["valid"] is False and "XML parse error" in r["errors"][0] and "checked" not in r


def test_malformed_xml_after_an_oversize_one_is_still_a_parse_error(big, monkeypatch):
    # lxml's parser log keeps entries from earlier failed parses in the same thread; classification must not read it
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    assert validate_xml(big)["verdict"] == "not-checkable-at-this-size"
    for bad in ("<X3D><Scene></X3D>", "<a><b></a>", "not xml at all"):
        r = validate_xml(bad)
        assert r["valid"] is False and "XML parse error" in r["errors"][0] and "checked" not in r, bad
    assert validate_xml(big)["verdict"] == "not-checkable-at-this-size"      # and the other way round


def test_is_resource_limit_reads_the_exception_not_the_log(big):
    with pytest.raises(etree.XMLSyntaxError) as limit:
        etree.fromstring(big.encode())
    with pytest.raises(etree.XMLSyntaxError) as plain:
        etree.fromstring(b"<a><b></a>")
    assert oversize.is_resource_limit(limit.value) and not oversize.is_resource_limit(plain.value)
    assert oversize.limit_kind(limit.value) == "start-tag"


DEEP = "<Group>" * 300 + "</Group>" * 300
TEXT = "<Script>" + "x" * 11_000_000 + "</Script>"


def wrap(body: str) -> str:
    return f'<X3D version="4.1" profile="Immersive"><Scene>{body}</Scene></X3D>'


def test_depth_limit_gets_its_own_explanation(monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    r = validate_xml(wrap(DEEP))
    assert r["verdict"] == "not-checkable-at-this-size" and r["checked"] is False and r["valid"] is False
    assert "deeper than libxml2's 256-level limit" in r["errors"][0] and "start tag" not in r["errors"][0]
    assert r["oversize"]["limit"] == "depth"


def test_text_node_limit_gets_its_own_explanation(monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    r = validate_xml(wrap(TEXT))
    assert r["verdict"] == "not-checkable-at-this-size" and "text node longer" in r["errors"][0] and "start tag" not in r["errors"][0]
    assert r["oversize"]["limit"] == "text-node"


def test_huge_on_lifts_the_depth_limit(monkeypatch):
    monkeypatch.setenv("X3D_MCP_XML_HUGE", "on")
    r = validate_xml(wrap(DEEP))
    assert r["valid"] is True and any("huge_tree" in n for n in r["notes"])


def test_ordinary_result_is_unchanged():
    r = validate_xml('<X3D version="4.1" profile="Interchange"><Scene><Shape><Box/></Shape></Scene></X3D>')
    assert r == {"valid": True, "errors": [], "schema_version": "4.1"}


def test_huge_is_off_by_default_and_says_how_to_turn_it_on(big, monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    assert oversize.huge_enabled() is False
    assert "X3D_MCP_XML_HUGE=on" in validate_xml(big)["oversize"]["huge_tree_retry"]


def test_huge_on_parses_and_validates(big, monkeypatch):
    monkeypatch.setenv("X3D_MCP_XML_HUGE", "on")
    r = validate_xml(big)
    assert r["valid"] is True and r["errors"] == [] and "checked" not in r
    assert any("huge_tree" in n for n in r["notes"])


def test_huge_on_still_reports_real_schema_errors(monkeypatch):
    monkeypatch.setenv("X3D_MCP_XML_HUGE", "on")
    r = validate_xml(splat_doc(90_000, extra_scene="<Shape><NoSuchNode/></Shape>\n"))
    assert r["valid"] is False and any("NoSuchNode" in e for e in r["errors"])


def test_huge_refuses_documents_with_a_doctype_or_entities(monkeypatch):
    monkeypatch.setenv("X3D_MCP_XML_HUGE", "on")
    lol = '<!DOCTYPE X3D [<!ENTITY a "aaaaaaaaaa"><!ENTITY b "&a;&a;&a;&a;&a;&a;&a;&a;&a;&a;">]>\n'
    r = validate_xml(splat_doc(90_000, prolog=lol))
    assert r["valid"] is False and r["verdict"] == "not-checkable-at-this-size"
    assert "DOCTYPE" in r["oversize"]["huge_tree_retry"]


def test_huge_refuses_documents_above_the_byte_cap(big, monkeypatch):
    monkeypatch.setenv("X3D_MCP_XML_HUGE", "on")
    monkeypatch.setenv("X3D_MCP_XML_HUGE_MAX_BYTES", "5000000")
    r = validate_xml(big)
    assert r["verdict"] == "not-checkable-at-this-size" and "X3D_MCP_XML_HUGE_MAX_BYTES=5,000,000" in r["oversize"]["huge_tree_retry"]


@pytest.mark.parametrize("value,expected", [("on", True), ("1", True), ("TRUE", True), (" yes ", True), ("off", False), ("", False), ("0", False), ("maybe", False)])
def test_huge_flag_parsing(monkeypatch, value, expected):
    monkeypatch.setenv("X3D_MCP_XML_HUGE", value)
    assert oversize.huge_enabled() is expected


def test_huge_max_bytes_falls_back_on_garbage(monkeypatch):
    monkeypatch.setenv("X3D_MCP_XML_HUGE_MAX_BYTES", "lots")
    assert oversize.huge_max_bytes() == oversize.DEFAULT_HUGE_MAX_BYTES


def test_parse_huge_reads_no_external_entities_or_dtd():
    host = Path("/etc/hostname").read_text().strip() if Path("/etc/hostname").exists() else "x3d-mcp-no-such-host"
    for doc in ('<!DOCTYPE r [<!ENTITY x SYSTEM "file:///etc/hostname">]><r>&x;</r>', '<!DOCTYPE r SYSTEM "file:///etc/hostname"><r/>'):
        assert host not in etree.tostring(oversize.parse_huge(doc)).decode()


def test_parse_huge_keeps_content_entities_unexpanded_but_attribute_entities_are_substituted():
    # why validate_xml refuses every DOCTYPE document before parse_huge: libxml2 substitutes internal entities in
    # attribute values whatever resolve_entities says (libxml2 2.14.6), and an attribute is where a splat array lives
    assert oversize.parse_huge('<!DOCTYPE r [<!ENTITY a "AAAA">]><r>&a;</r>').text is None
    assert oversize.parse_huge('<!DOCTYPE r [<!ENTITY a "AAAA">]><r x="&a;"/>').get("x") == "AAAA"


def test_find_big_start_tags_ignores_small_tags_and_comments():
    text = '<a x="1"/>' + "<!-- " + "<b " + "y " * 600000 + " -->" + '<c z="' + "1 " * 700000 + '"/>'
    tags = oversize.find_big_start_tags(text)
    assert [t.name for t in tags] == ["c"] and tags[0].size > 1_000_000
    s, e = tags[0].attrs["z"]
    assert text[s:e].startswith("1 1 1") and e - s == 1_400_000


def test_find_big_start_tags_handles_single_quotes_and_line_numbers():
    text = "<r>\n\n<n a='" + "7 " * 600000 + "' b=\"x\"/></r>"
    t = oversize.find_big_start_tags(text)[0]
    assert t.line == 3 and set(t.attrs) == {"a", "b"}


def test_reduced_document_swaps_only_big_values():
    text = '<r><n a="' + "1 " * 40000 + '" b="2 3"/></r>'
    tags = oversize.find_big_start_tags(text, min_bytes=1000)
    out, skipped = oversize.reduced_document(text, tags, lambda node, attr: "MFVec3f")
    assert skipped == [] and out == '<r><n a="0 0 0" b="2 3"/></r>'
    out2, skipped2 = oversize.reduced_document(text, tags, lambda node, attr: "SFNode")
    assert skipped2 == ["n.a"] and out2 == text


def test_without_numpy_the_counts_are_still_checked_and_the_values_are_flagged_unchecked(tmp_path):
    # numpy is an optional extra; with it missing the verdict and the count checks still work
    import subprocess
    src = str(Path(__file__).resolve().parent.parent / "src")
    doc = tmp_path / "big.x3d"
    doc.write_text(with_bad_token(splat_doc(90_000, wrong_count="scales")))
    code = ("import sys, json; sys.modules['numpy'] = None; sys.path.insert(0, %r)\n"
            "from validation.validate import validate_xml\n"
            "r = validate_xml(open(%r).read())\n"
            "t = r['oversize']['tags'][0]['array_check']\n"
            "print(json.dumps({'verdict': r['verdict'], 'errors': t['errors'], 'warnings': t['warnings']}))\n") % (src, str(doc))
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120)
    assert out.returncode == 0, out.stderr[-500:]
    got = json.loads(out.stdout.strip().splitlines()[-1])
    assert got["verdict"] == "not-checkable-at-this-size"
    assert any("scales" in e for e in got["errors"])                      # the count mismatch is found without numpy
    assert "orientations: not a number: 'abc'." in got["errors"]          # and so is a word where a number belongs
    assert any("numpy is not installed" in w for w in got["warnings"])


# ---- the start-tag scanner and the hostile inputs that once broke it

def test_parse_start_tag_reads_attribute_spans():
    text = "xx<Node a=\"1 2\" b='3' c = \"4\" />yy"
    name, attrs, end = oversize.parse_start_tag(text, 2)
    assert name == "Node" and text[end - 2:end] == "/>" and text[end:] == "yy"
    assert {k: text[s:e] for k, (s, e) in attrs.items()} == {"a": "1 2", "b": "3", "c": "4"}
    assert oversize.parse_start_tag("<a>", 0) == ("a", {}, 3)
    assert oversize.parse_start_tag("<a/>", 0) == ("a", {}, 4)


@pytest.mark.parametrize("bad", ['<a b="1"', '<a b=1>', '<a b="1"c="2">', '<a b="<">', "<a b='1\"", "<1a>", "< a>", '<a b="1" c>', '<a "1">'])
def test_parse_start_tag_refuses_what_is_not_a_start_tag(bad):
    assert oversize.parse_start_tag(bad, 0) is None


def test_parse_start_tag_caps_the_attribute_count():
    many = "<a " + " ".join(f'k{i}="1"' for i in range(oversize.MAX_ATTRS)) + "/>"
    assert oversize.parse_start_tag(many, 0) is not None
    too_many = "<a " + " ".join(f'k{i}="1"' for i in range(oversize.MAX_ATTRS + 1)) + "/>"
    assert oversize.parse_start_tag(too_many, 0) is None


def test_scanner_costs_flat_memory_on_a_million_attributes():
    import time, tracemalloc
    text = "<GaussianSplats " + 'a="1" ' * 1_000_000           # never closed; the regex version held ~580 bytes per attribute
    tracemalloc.start(); t = time.perf_counter()
    assert oversize.parse_start_tag(text, 0) is None and oversize.find_big_start_tags(text) == []
    dt = time.perf_counter() - t; peak = tracemalloc.get_traced_memory()[1]; tracemalloc.stop()
    assert peak < 5_000_000 and dt < 2


def test_unterminated_oversize_tag_is_not_called_well_formed(monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    doc = '<X3D version="4.1" profile="Immersive"><Scene><WorldInfo ' + 'a="1" ' * 1_800_000          # 10.8 MB, never closed
    r = validate_xml(doc)
    assert r["verdict"] == "not-checkable-at-this-size" and "may also be malformed" in r["errors"][0]
    assert "not a well-formedness error" not in r["errors"][0]
    assert r["oversize"]["structure_check"]["ran"] is False


def test_oversize_tag_made_of_many_mid_sized_attributes_does_not_recurse(monkeypatch):
    # each value is under the substitution threshold, so the reduced copy is still over the limit; this once raised RecursionError
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    doc = '<X3D version="4.1" profile="Immersive"><Scene><WorldInfo ' + " ".join(f'm{i}="{"1" * 55000}"' for i in range(200)) + "/></Scene></X3D>"
    r = validate_xml(doc)
    sc = r["oversize"]["structure_check"]
    assert r["verdict"] == "not-checkable-at-this-size" and sc["ran"] is False and "start tag larger than" in sc["reason"]


def test_reentry_flag_is_cleared_after_every_call(big, monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    from validation import validate as v
    validate_xml(big)
    assert getattr(v._reentry, "active", False) is False
    validate_xml(big)                                   # a second call still runs the structure check
    assert validate_xml(big)["oversize"]["structure_check"]["ran"] is True


def test_array_value_check_is_skipped_above_the_byte_cap(big, monkeypatch):
    monkeypatch.delenv("X3D_MCP_XML_HUGE", raising=False)
    monkeypatch.setattr(oversize, "MAX_VALUE_BYTES", 1_000_000)
    check = validate_xml(big)["oversize"]["tags"][0]["array_check"]
    assert check["errors"] == [] and any("too large to parse" in w for w in check["warnings"])
    assert validate_xml(big)["oversize"]["tags"][0]["splats"] == 90_000        # counts are still reported


def test_count_numbers_matches_a_split_and_builds_no_list():
    from validation.splat_arrays import count_numbers
    for text in ("", "  ", "1", "1 2 3", "1,2 ,3,, 4\n5\t6", ",,,", " 1e-3,+2.5 "):
        assert count_numbers(text) == len([t for t in text.replace(",", " ").split() if t]), text
