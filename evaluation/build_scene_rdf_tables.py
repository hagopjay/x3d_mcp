"""Build src/validation/schemas/X3dToTurtle-tables.json, the rule tables behind x3d_utils.scene_rdf.

scene_rdf converts X3D to Turtle the way the Web3D Consortium stylesheet X3dToTurtle.xslt does, without
an XSLT 2.0 processor. Two things in the stylesheet are data rather than structure, and both are copied
here so the converter follows them exactly:

  1. Its XPath tests on each attribute: which attributes are left out (default values, implicit events)
     and which X3D field type an attribute has (that decides bare number, rdf:List or string).
     The test texts are copied verbatim; x3d_utils.xpath_mini evaluates them.
  2. The attribute defaults of the X3D DTDs. The official build parses scenes with DTD defaults
     expanded (Saxon -expand:on; lxml attribute_defaults=True in the lab runner), so every node carries
     its default containerField and the stylesheet writes x3do:hasGeometry, not x3do:hasSphere.

Usage (sources are the Consortium files; the SVN revisions used for the shipped table are in its header):
  python evaluation/build_scene_rdf_tables.py X3dToTurtle.xslt DTD_DIR [--out PATH]
DTD_DIR holds x3d-3.3.dtd, x3d-4.0.dtd and x3d-4.1.dtd.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

from lxml import etree

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from x3d_utils.xpath_mini import compile_expr, variables  # noqa: E402

XSL = "http://www.w3.org/1999/XSL/Transform"
NS = {"xsl": XSL}
OUT = ROOT / "src" / "validation" / "schemas" / "X3dToTurtle-tables.json"
DTD_VERSIONS = ("3.3", "4.0", "4.1")

# Expected texts of the two variables that walk ancestors; scene_rdf computes them in Python, so a
# stylesheet change to either must stop the build.
HANIM_EXPECTED = {
    "isHAnim1": "$isX3D3 and ancestor-or-self::*[local-name() = 'HAnimHumanoid'][starts-with(@version,'1') "
                "or (string-length(@version) = 0)]",
    "isHAnim2": "$isX3D4 and ancestor-or-self::*[local-name() = 'HAnimHumanoid'][starts-with(@version,'2')] "
                "and not($isHAnim1 = true())",
}
SIGNATURE = "(count(//ds:Signature) > 0)"
# Node-set tests in the rule texts that the subset cannot walk: replaced by a variable scene_rdf computes.
PATH_VARS = {"../IS/connect[@nodeField='whichChoice']": "$parentISConnectsWhichChoice",
             "//X3D/@version": "$x3dVersion"}

LICENSE = ("Rule texts copied from X3dToTurtle.xslt and attribute defaults read from the X3D DTDs, "
           "Copyright (c) 1995-2026 held by the author(s), Web3D Consortium Open-Source License for Models "
           "and Software (BSD-style): redistribution with this notice; no endorsement implied. "
           "https://www.web3d.org/x3d/content/examples/license.html")


def norm(text: str) -> str:
    return " ".join(text.split())


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def extract_rules(xslt_path: Path) -> dict:
    doc = etree.parse(str(xslt_path))
    main = doc.find(".//xsl:template[@match='*']", NS)
    loop = main.find(".//xsl:for-each[@select=\"@*[(local-name() != 'containerField')]\"]", NS)
    if loop is None:
        raise SystemExit("attribute loop not found in the main template")
    keep_vars, hanim = {}, {}
    for var in loop.findall("xsl:variable", NS):
        name, select = var.get("name"), norm(var.get("select") or "")
        if name in HANIM_EXPECTED:
            if select != norm(HANIM_EXPECTED[name]):
                raise SystemExit(f"${name} changed in the stylesheet: {select}")
            hanim[name] = select
            continue
        for path, var in PATH_VARS.items():
            select = select.replace(path, var)
        keep_vars[name] = select
    keep_if = loop.find("xsl:if", NS)
    keep_test = norm(keep_if.get("test"))
    if SIGNATURE not in keep_test:
        raise SystemExit("signature test not found in the keep test")
    keep_test = keep_test.replace(SIGNATURE, "($signatureCount > 0)")

    attr_type = doc.find(".//xsl:template[@name='attribute-type']", NS)
    choose = attr_type.findall("xsl:choose", NS)[-1]
    type_rules = []
    for when in choose.findall("xsl:when", NS):
        text = when.find("xsl:text", NS)
        value_of = when.find("xsl:value-of", NS)
        if text is not None:
            result = text.text.strip()
        elif value_of is not None and value_of.get("select") == "$localFieldType":
            result = "$localFieldType"
        else:
            raise SystemExit("unexpected xsl:when body in attribute-type")
        test = norm(when.get("test"))
        for path, var in PATH_VARS.items():
            test = test.replace(path, var)
        type_rules.append([test, result])

    # Dublin Core names the meta loop maps (first xsl:when of the dcterm choose), and the renamed ones.
    meta_choose = main.find(".//xsl:variable[@name='dcterm']/xsl:choose", NS)
    whens = meta_choose.findall("xsl:when", NS)
    dc_same = re.findall(r"lower-case\(@name\) = '([A-Za-z]+)'", whens[0].get("test"))
    dc_renamed = {}
    for when in whens[1:]:
        names = re.findall(r"lower-case\(@name\) = '([A-Za-z]+)'", when.get("test") or "")
        text = when.find("xsl:text", NS)
        if names and text is not None and text.text and text.text.strip():
            for n in names:
                dc_renamed[n] = text.text.strip()

    rules = {"keep_vars": keep_vars, "keep_test": keep_test, "type_rules": type_rules,
             "hanim_vars": hanim, "dcterms_same": dc_same, "dcterms_renamed": dc_renamed}
    # every text must parse in the supported subset, and read only the variables scene_rdf provides
    allowed = set(keep_vars) | {"isHAnim1", "isHAnim2", "isX3D3", "isX3D4", "signatureCount",
                                "parentISConnectsWhichChoice", "x3dVersion",
                                "parentElementName", "attributeName", "attributeNameFull", "localFieldType"}
    for name, text in list(keep_vars.items()) + [("keep_test", keep_test)] + [(r[1], r[0]) for r in type_rules]:
        used = variables(compile_expr(text))
        if used - allowed:
            raise SystemExit(f"{name} reads unknown variables {sorted(used - allowed)}")
    return rules


def extract_dtd(path: Path) -> dict:
    dtd = etree.DTD(str(path))
    elements = {}
    for el in dtd.elements():
        defaults, tokenized = {}, []
        for a in el.attributes():
            if a.prefix == "xmlns":
                continue
            qname = f"{a.prefix}:{a.name}" if a.prefix else a.name
            if a.type != "cdata":
                tokenized.append(qname)
            if a.default in ("none", "fixed") and a.default_value is not None:
                defaults[qname] = a.default_value
        elements[el.name] = {"defaults": dict(sorted(defaults.items())), "tokenized": sorted(tokenized)}
    return dict(sorted(elements.items()))


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("xslt", type=Path)
    ap.add_argument("dtd_dir", type=Path)
    ap.add_argument("--out", type=Path, default=OUT)
    args = ap.parse_args(argv)
    sources = {"X3dToTurtle.xslt": {"sha256": sha256(args.xslt), "svn": "r40388 (2026-10-29)"}}
    dtds = {}
    for v in DTD_VERSIONS:
        p = args.dtd_dir / f"x3d-{v}.dtd"
        dtds[v] = extract_dtd(p)
        sources[p.name] = {"sha256": sha256(p)}
    table = {
        "about": "Rule tables for x3d_utils.scene_rdf, generated by evaluation/build_scene_rdf_tables.py",
        "license": LICENSE,
        "sources": sources,
        "rules": extract_rules(args.xslt),
        "dtd": dtds,
    }
    text = json.dumps(table, indent=1, ensure_ascii=False, sort_keys=False) + "\n"
    args.out.write_text(text, encoding="utf-8")
    n_rules = len(table["rules"]["keep_vars"]) + len(table["rules"]["type_rules"]) + 1
    print(f"wrote {args.out} ({len(text):,} bytes): {n_rules} rule texts, "
          + ", ".join(f"DTD {v}: {len(d)} elements" for v, d in dtds.items()))


if __name__ == "__main__":
    main()
