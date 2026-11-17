"""Checks by layer: run each case's SHACL shapes on the fault variant with (a) domain only (b) scene+domain. Count results."""
import json, sys
from pathlib import Path
import rdflib, pyshacl
from x3d_utils import semantic_web as sw
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT / "build"))
import cases as D
SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
def run(g, shapes):
    _, rg, _ = pyshacl.validate(g, shacl_graph=rdflib.Graph().parse(data=shapes, format="turtle"), inference="none", advanced=True)
    return sorted(str(rg.value(r, SH.focusNode)).rsplit("#",1)[-1].rsplit("/",1)[-1] for r in rg.subjects(rdflib.RDF.type, SH.ValidationResult))
out = {}
for name, s in D.SCEN.items():
    tw = rdflib.Graph().parse(data=sw.convert((ROOT/"scenes"/f"{name}-fault.x3d").read_text()).turtle, format="turtle")
    dm = rdflib.Graph().parse(data=(ROOT/"data"/name/"domain-fault.ttl").read_text(), format="turtle")
    out[name] = {"both": run(tw + dm, s["shapes"]), "domain_only": run(dm, s["shapes"])}
    print(name, out[name])
L = json.loads((ROOT/"eval"/"layers.json").read_text())
for n in out: L[n]["checks"] = out[n]
(ROOT/"eval"/"layers.json").write_text(json.dumps(L, indent=1))
