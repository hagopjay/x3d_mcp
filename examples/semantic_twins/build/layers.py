"""Where does each answer live? Run every question and rule on the scene twin alone, the domain graph alone and both,
and every check on clean+fault. Writes data/layers.json."""
import json, sys
from pathlib import Path
import rdflib
from x3d_utils import semantic_web as sw
ROOT = Path(__file__).resolve().parents[1]; sys.path.insert(0, str(ROOT / "build"))
import cases as D
def rows(g, q):
    try: return sorted(tuple(str(v) for v in r) for r in g.query(q))
    except Exception as e: return "ERR " + str(e)[:80]
out = {}
for name, s in D.SCEN.items():
    tw = rdflib.Graph().parse(data=sw.convert((ROOT/"scenes"/f"{name}.x3d").read_text()).turtle, format="turtle")
    dm = rdflib.Graph().parse(data=(ROOT/"data"/name/"domain.ttl").read_text(), format="turtle")
    both = tw + dm
    pre = D.sparql_prefixes(s["pfx"]); res = []
    for q in s["q"] + [dict(s["inf"], id="INF")]:
        gold = rows(both, pre + q["sparql"])
        a, b = rows(tw, pre + q["sparql"]), rows(dm, pre + q["sparql"])
        res.append({"id": q["id"], "text": q.get("text", q.get("q", "")), "gold": len(gold),
                    "scene_only": a == gold, "domain_only": b == gold, "scene_rows": len(a) if isinstance(a, list) else a,
                    "domain_rows": len(b) if isinstance(b, list) else b})
    out[name] = {"twin_triples": len(tw), "domain_triples": len(dm), "questions": res}
(ROOT/"eval"/"layers.json").write_text(json.dumps(out, indent=1))
tot = sum(len(v["questions"]) for v in out.values())
print("questions+rules", tot, "scene-only right", sum(q["scene_only"] for v in out.values() for q in v["questions"]),
      "domain-only right", sum(q["domain_only"] for v in out.values() for q in v["questions"]))
for n, v in out.items(): print(n, v["twin_triples"], v["domain_triples"], [(q["id"], q["gold"], q["scene_rows"], q["domain_rows"], q["domain_only"]) for q in v["questions"]])
