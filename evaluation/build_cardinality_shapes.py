"""Build the x3d-cardinality SHACL shapes: an SFNode field holds at most one node.

The XSD does not count children per field, and the X3D Ontology gives no cardinality, so two Material
nodes in one Appearance pass both. The X3DUOM knows each field's type. For every concrete node this
writes one node shape whose property shapes say sh:maxCount 1 on has<Field> for each SFNode field the
node can hold in a file (initializeOnly or inputOutput; IS is a statement, not a field).

Fields are read from the X3DUOM 4.1, the release shacl_validate reads for every document version. A
field is skipped, and counted in the header, when the bundled X3D Ontology 4.1 does not declare its
has<Field> property (the converter then never writes it).

    python evaluation/build_cardinality_shapes.py            # rewrites the shapes file
    python evaluation/build_cardinality_shapes.py --check    # exit 1 if the file is out of date
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import rdflib  # noqa: E402

from x3d_utils.x3duom import get_x3duom  # noqa: E402

OUT = ROOT / "src" / "validation" / "schemas" / "shapes" / "x3d-cardinality.shacl.ttl"
ONTOLOGY = ROOT / "src" / "validation" / "schemas" / "X3dOntology4.1.ttl"
NS = "https://www.web3d.org/specifications/X3dOntology4.1#"
FILE_ACCESS = ("initializeOnly", "inputOutput")


def prop(field: str) -> str:
    return "has" + field[:1].upper() + field[1:]


def build() -> str:
    onto = rdflib.Graph()
    onto.parse(str(ONTOLOGY), format="turtle")
    declared = {str(s)[len(NS):] for s in set(onto.subjects()) if str(s).startswith(NS)}
    nodes = get_x3duom().get_concrete_nodes()
    per_node: dict[str, list[str]] = {}
    skipped: set[str] = set()
    for name in sorted(nodes):
        if name not in declared:
            continue
        fields = sorted(f["name"] for f in nodes[name]["fields"]
                        if f.get("type") == "SFNode" and f.get("accessType") in FILE_ACCESS and f["name"] != "IS")
        keep = [f for f in fields if prop(f) in declared]
        skipped.update(f for f in fields if prop(f) not in declared)
        if keep:
            per_node[name] = keep
    used = sorted({f for fs in per_node.values() for f in fs})
    pairs = sum(len(v) for v in per_node.values())
    lines = [
        "# x3d-cardinality: an SFNode field holds at most one node. GENERATED, do not edit:",
        "#   python evaluation/build_cardinality_shapes.py",
        f"# Source: X3DUOM 4.1 field types. {len(per_node)} node shapes, {len(used)} property shapes, {pairs} (node, field) pairs.",
        f"# Fields skipped because the X3D Ontology 4.1 declares no has<Field> property: {', '.join(sorted(skipped)) or 'none'}.",
        "# A USE counts: it occupies the field. validate_semantic reports the same as sfnode-multiple.",
        "",
        "@prefix sh:   <http://www.w3.org/ns/shacl#> .",
        "@prefix rdfs: <http://www.w3.org/2000/01/rdf-schema#> .",
        "@prefix owl:  <http://www.w3.org/2002/07/owl#> .",
        f"@prefix x3do: <{NS}> .",
        "@prefix xs:   <urn:x3d-mcp:shapes:x3d-core#> .",
        "@prefix xc:   <urn:x3d-mcp:shapes:x3d-cardinality#> .",
        "",
        'xc: a owl:Ontology ; rdfs:label "x3d-cardinality shapes" ; owl:versionInfo "1" .',
        "",
    ]
    for f in used:
        lines += [
            f"xc:one_{f} a sh:PropertyShape ; sh:path x3do:{prop(f)} ; sh:maxCount 1 ;",
            f'  sh:message "{f} is an SFNode field: it holds one node, and this node has more than one child filed there" ;',
            '  xs:checkId "sfnode-multiple" ;',
            f'  xs:fixHint "Keep one node in {f}; remove the others or give each the containerField of the field it is meant for (modify_x3d_node)." .',
        ]
    lines.append("")
    for name, fields in per_node.items():
        lines.append(f"xc:{name} a sh:NodeShape ; sh:targetClass x3do:{name} ; sh:property "
                     + " , ".join(f"xc:one_{f}" for f in fields) + " .")
    return "\n".join(lines) + "\n"


def main() -> int:
    text = build()
    if "--check" in sys.argv:
        if not OUT.exists() or OUT.read_text(encoding="utf-8") != text:
            print(f"{OUT.name} is out of date; run evaluation/build_cardinality_shapes.py")
            return 1
        print(f"{OUT.name} is up to date")
        return 0
    OUT.write_text(text, encoding="utf-8")
    print(text.splitlines()[2])
    print(text.splitlines()[3])
    return 0


if __name__ == "__main__":
    sys.exit(main())
