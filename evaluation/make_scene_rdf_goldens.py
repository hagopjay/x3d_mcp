"""Regenerate the scene_to_rdf golden Turtle with the Consortium's own pipeline (development only).

    python evaluation/make_scene_rdf_goldens.py X3dToTurtle.xslt DTD_DIR [--out tests/fixtures/scene_rdf] [X3D ...]

The golden for each .x3d is what X3dToTurtle.xslt (run by Saxon-HE, via saxonche) writes for the document
after the X3D DTD has filled in its attribute defaults (containerField and the others), which is what the
Consortium's build does when it reads a document with its DOCTYPE. lxml loads the DTD named for the
document's version from DTD_DIR (x3d-3.3.dtd, x3d-4.0.dtd, x3d-4.1.dtd), without network access.

saxonche is not a dependency of the server: without it this script says so and exits 0, and the test
that calls it is skipped. The server's converter (src/x3d_utils/scene_rdf.py) never uses Saxon;
tests/test_semantic_web_tools.py compares its graphs with the goldens written here (rdflib isomorphism).

With no X3D arguments it regenerates every .x3d under --out (including faulted/). It writes <stem>.golden.ttl
next to each input and manifest.json with the sha256 of the stylesheet, the DTDs and every file.
"""

import argparse
import hashlib
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUT = ROOT / "tests" / "fixtures" / "scene_rdf"
_VERSION = re.compile(r"<X3D[^>]*\sversion\s*=\s*(['\"])(.*?)\1", re.S)
_DOCTYPE = re.compile(r"<!DOCTYPE[^>]*>")
_XML_DECL = re.compile(r"^\s*<\?xml[^>]*\?>")


def sha256_file(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def dtd_for(text: str, dtd_dir: Path) -> Path:
    """The DTD for the document's version, by the converter's rule (scene_rdf.dtd_version): 3.x reads
    the 3.3 DTD, 4.0 the 4.0 DTD, and 4.1 or anything else the 4.1 DTD. version is #REQUIRED in every
    X3D DTD, so a document without a valid one has no official DTD; 4.1 is what the validators assume."""
    m = _VERSION.search(text)
    version = m.group(2).strip() if m else ""
    name = "3.3" if version.startswith("3") else ("4.0" if version == "4.0" else "4.1")
    return (dtd_dir / f"x3d-{name}.dtd").resolve()


def with_defaults(text: str, dtd: Path):
    """Parse with the DTD applied (attribute defaults injected); no network, no external entities."""
    from lxml import etree

    body = _DOCTYPE.sub("", _XML_DECL.sub("", text, count=1), count=1).lstrip()
    doc = f'<!DOCTYPE X3D SYSTEM "{dtd.as_uri()}">\n' + body
    parser = etree.XMLParser(load_dtd=True, attribute_defaults=True, no_network=True, huge_tree=True,
                             resolve_entities=False)
    return etree.tostring(etree.fromstring(doc.encode("utf-8"), parser), encoding="unicode")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("xslt", type=Path, help="X3dToTurtle.xslt")
    ap.add_argument("dtd_dir", type=Path, help="directory with x3d-3.3.dtd, x3d-4.0.dtd, x3d-4.1.dtd")
    ap.add_argument("inputs", nargs="*", type=Path)
    ap.add_argument("--out", type=Path, default=DEFAULT_OUT, help="fixture directory (manifest.json goes here)")
    args = ap.parse_args(argv)
    try:
        from saxonche import PySaxonProcessor
    except ImportError:
        print("saxonche is not installed: goldens not regenerated (pip install saxonche)")
        return 0
    import lxml
    import saxonche

    inputs = args.inputs or sorted(args.out.rglob("*.x3d"))
    manifest = {
        "pipeline": "lxml DTD attribute defaults, then X3dToTurtle.xslt with Saxon-HE (saxonche)",
        "stylesheet_sha256": sha256_file(args.xslt),
        "dtd_sha256": {p.name: sha256_file(p) for p in sorted(args.dtd_dir.glob("x3d-[34].[0-9].dtd"))},
        "saxonche": getattr(saxonche, "__version__", None) or _dist_version("saxonche"),
        "lxml": lxml.__version__,
        "files": {},
    }
    with PySaxonProcessor(license=False) as proc:
        executable = proc.new_xslt30_processor().compile_stylesheet(stylesheet_file=str(args.xslt))
        for src in inputs:
            text = src.read_text(encoding="utf-8")
            dtd = dtd_for(text, args.dtd_dir)
            node = proc.parse_xml(xml_text=with_defaults(text, dtd))
            turtle = executable.transform_to_string(xdm_node=node) or ""
            out = src.with_name(src.stem + ".golden.ttl")
            out.write_text(turtle, encoding="utf-8")
            rel = src.resolve().relative_to(args.out.resolve()).as_posix()
            manifest["files"][rel] = {"x3d_sha256": sha256_file(src), "dtd": dtd.name,
                                      "golden": out.name, "golden_sha256": sha256_file(out)}
            print(f"{rel}: {dtd.name} -> {out.name} ({len(turtle)} characters)")
    (args.out / "manifest.json").write_text(json.dumps(manifest, indent=1) + "\n", encoding="utf-8")
    return 0


def _dist_version(name: str):
    try:
        from importlib.metadata import version
        return version(name)
    except Exception:
        return None


if __name__ == "__main__":
    sys.exit(main())
