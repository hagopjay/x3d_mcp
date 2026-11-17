"""Python oracle: X3D twin (x3d_mcp converter) + domain graph -> questions, inference, SHACL. Writes data/<s>/oracle.json.
Run from the repo's src/ directory (needs rdflib, pyshacl and x3d_utils)."""
import json, sys, time
from pathlib import Path
import rdflib, pyshacl
from x3d_utils import semantic_web as sw

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
import cases as D


def short(v):
    s = str(v)
    for sep in ("#",):
        if sep in s and s.startswith("http"):
            return s.rsplit(sep, 1)[1]
    return s.rsplit("/", 1)[-1] if s.startswith("http") else s


def run(name, fault):
    s = D.SCEN[name]
    scene = (ROOT / "scenes" / f"{name}{'-fault' if fault else ''}.x3d").read_text()
    sg = sw.convert(scene)
    g = rdflib.Graph()
    g.parse(data=sg.turtle, format="turtle")
    n_twin = len(g)
    dom = (ROOT / "data" / name / ("domain-fault.ttl" if fault else "domain.ttl")).read_text()
    g.parse(data=dom, format="turtle")
    out = {"twin_triples": n_twin, "domain_triples": len(g) - n_twin, "questions": {}, "inference": None, "shacl": []}
    pre = D.sparql_prefixes(s["pfx"])
    for q in s["q"]:
        rows = g.query(pre + q["sparql"])
        out["questions"][q["id"]] = [{str(k): short(r[k]) if r[k] is not None else None for k in rows.vars} for r in rows]
    rows = g.query(pre + s["inf"]["sparql"])
    out["inference"] = [{str(k): short(r[k]) for k in rows.vars} for r in rows]
    shapes = rdflib.Graph().parse(data=s["shapes"], format="turtle")
    t0 = time.time()
    conforms, rg, _ = pyshacl.validate(g, shacl_graph=shapes, inference="none", advanced=True)
    out["shacl_seconds"] = round(time.time() - t0, 2)
    SH = rdflib.Namespace("http://www.w3.org/ns/shacl#")
    for r in rg.subjects(rdflib.RDF.type, SH.ValidationResult):
        out["shacl"].append({"focus": short(rg.value(r, SH.focusNode)), "value": short(rg.value(r, SH.value)),
                             "shape": short(rg.value(r, SH.sourceShape)), "message": str(rg.value(r, SH.resultMessage))})
    out["shacl"].sort(key=lambda x: (x["shape"], x["focus"]))
    out["conforms"] = bool(conforms)
    return out


if __name__ == "__main__":
    res = {}
    for name in D.SCEN:
        res[name] = {"clean": run(name, False), "fault": run(name, True)}
        (ROOT / "data" / name / "oracle.json").write_text(json.dumps(res[name], indent=1))
    print(json.dumps(res, indent=1))
