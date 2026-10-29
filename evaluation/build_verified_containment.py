"""Build the verified containment table the exact-fix hints use.

For every (parent, field, child) that X3DUOM allows (the child, or one of its
abstract ancestors, is in the parent field's acceptableNodeTypes), place the
child under the parent in a minimal document and validate it against the
bundled XSD of each version. The verdict is:

  A  accepted: the document is valid
  R  rejected: the first error is the XSD refusing the child there ("This
     element is not expected", or its containerField value is not allowed)
  I  inconclusive: invalid for another reason (the harness could not place the
     parent, or the parent needs other content)

Only the exceptions (R and I) are written, keyed "Parent.field", so the table
stays small: every UOM-allowed pair not listed is XSD-verified (A). Run from
the repository root:

    uv run python evaluation/build_verified_containment.py

and it rewrites src/validation/schemas/verified-containment.json.
"""

import json
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "evaluation"))

from cross_validate_ontology import minimal_doc  # noqa: E402
from validation.semantic import _type_ancestry, _field_accepts  # noqa: E402
from validation.validate import SCHEMAS_DIR, XSD_PATHS, validate_xml  # noqa: E402
from x3d_utils.x3duom import X3DUOM  # noqa: E402

OUT = SCHEMAS_DIR / "verified-containment.json"


def uom_pairs(uom: X3DUOM):
    nodes = uom.get_concrete_nodes()
    abstracts = uom.get_abstract_types()
    anc = {c: _type_ancestry(c, nodes, abstracts) for c in nodes}
    for parent in sorted(nodes):
        for f in nodes[parent]["fields"]:
            if f.get("type") not in ("SFNode", "MFNode") or not f.get("acceptableNodeTypes"):
                continue
            for child in sorted(nodes):
                if _field_accepts(f, anc[child]):
                    yield parent, f["name"], child


def build(version: str) -> dict:
    uom = X3DUOM(SCHEMAS_DIR / f"X3dUnifiedObjectModel-{version}.xml")
    nodes = uom.get_concrete_nodes()
    t0 = time.time()
    totals = {"A": 0, "R": 0, "I": 0}
    exceptions: dict[str, dict[str, list[str]]] = {}
    for parent, field, child in uom_pairs(uom):
        default_cf = nodes[child].get("containerField")
        cf_attr = None if field == default_cf else field
        doc = minimal_doc(parent, child, cf_attr, nodes[parent].get("containerField"))
        doc = doc.replace("version='4.1'", f"version='{version}'")
        v = validate_xml(doc)
        if v["valid"]:
            verdict = "A"
        elif (f"Element '{child}': This element is not expected" in v["errors"][0]
              or f"Element '{child}', attribute 'containerField'" in v["errors"][0]):
            verdict = "R"
        else:
            verdict = "I"
        totals[verdict] += 1
        if verdict != "A":
            exceptions.setdefault(f"{parent}.{field}", {}).setdefault(verdict, []).append(child)
    return {"seconds": round(time.time() - t0, 1), "uom_allowed_pairs": sum(totals.values()),
            "totals": totals, "exceptions": exceptions}


def main() -> None:
    table = {
        "about": "XSD verdicts for every X3DUOM-allowed (parent, field, child); only R "
                 "(XSD rejects) and I (inconclusive) are listed, keyed 'Parent.field'. "
                 "Built by evaluation/build_verified_containment.py.",
        "versions": {v: build(v) for v in sorted(XSD_PATHS)},
    }
    OUT.write_text(json.dumps(table, separators=(",", ":"), sort_keys=True) + "\n")
    for v, d in table["versions"].items():
        print(v, d["uom_allowed_pairs"], d["totals"], f"{d['seconds']}s")
    print("wrote", OUT.relative_to(ROOT), OUT.stat().st_size, "bytes")


if __name__ == "__main__":
    main()
