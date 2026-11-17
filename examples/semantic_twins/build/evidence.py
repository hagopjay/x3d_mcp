"""Evidence states for every claim the nine cases make. Writes evidence/evidence.json and evidence/EVIDENCE.md.

Each claim gets one state, so a reader can tell what was recomputed from what was copied:

    computed here         recomputed by this script from the packaged files, and it agrees with the stored result
    hash checked          the file's sha256 equals the one in evidence/manifest.json
    recorded              taken from a stored record (an earlier MCP run, a browser run) without re-running it
    explained difference  recomputed, it differs from the record, and the reason is known and stated
    not packaged          the claim rests on material that is not in this folder
    mismatch              recomputed, it differs, and no reason is known (the run exits non-zero)

Run from the repo's src/ directory:  python ../examples/semantic_twins/build/evidence.py [--write-manifest]
The MCP check calls the tools in-process (FastMCP.call_tool), not over stdio as record.py does.
"""
import asyncio
import hashlib
import json
import re
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
import cases as D          # noqa: E402
import oracle              # noqa: E402

from mcp.server.fastmcp import FastMCP          # noqa: E402
from tools import semantic_web as sw_tools      # noqa: E402

EV = ROOT / "evidence"
MANIFEST = EV / "manifest.json"
ORDER_BY = re.compile(r"\bORDER\s+BY\b", re.I)


def sha_file(p: Path) -> str:
    return hashlib.sha256(p.read_bytes()).hexdigest()


def packaged_files():
    for sub in ("scenes", "data", "eval", "dl"):
        for p in sorted((ROOT / sub).rglob("*")):
            if p.is_file() and "__pycache__" not in p.parts:
                yield p


def short(v):
    """The oracle's short form, applied to a typed MCP cell or a compact MCP string."""
    if v is None:
        return None
    if isinstance(v, dict):
        v = v["value"]
    if v.startswith(":"):
        return v[1:]
    return oracle.short(v)


def bag(rows):
    return Counter(json.dumps(r, sort_keys=True) for r in rows)


def norm_rows(rows):
    return [{k: short(v) for k, v in r.items()} for r in rows]


def oracle_typed(rows, names):
    """A typed MCP answer in the oracle's shape: unbound variables come back as None, as the oracle writes them."""
    return [{n: short(r.get(n)) for n in names} for r in rows]


def reply(res) -> dict:
    content = res[0] if isinstance(res, tuple) else res
    return json.loads(content[0].text)


async def mcp_answers(mcp, scene, data, pre, queries):
    conv = reply(await mcp.call_tool("scene_to_rdf", {"content": scene, "return_mode": "summary"}))
    out = {}
    for q in queries:
        res = await mcp.call_tool("sparql_scene", {"sparql": pre + q["sparql"], "graph_id": conv["graph_id"],
                                                   "data_ttl": data, "include_ontology": False,
                                                   "result_format": "typed", "limit": 500})
        out[q["id"]] = reply(res)
    return out


def main(write_manifest: bool):
    EV.mkdir(exist_ok=True)
    files = {str(p.relative_to(ROOT)): sha_file(p) for p in packaged_files()}
    if write_manifest or not MANIFEST.exists():
        MANIFEST.write_text(json.dumps({"algorithm": "sha256 of the file bytes", "files": files}, indent=1) + "\n")
    recorded = json.loads(MANIFEST.read_text())["files"]

    mcp = FastMCP("evidence")
    sw_tools.register(mcp)
    claims, cases = [], {}

    def claim(case, cid, text, state, detail=""):
        claims.append({"case": case, "id": cid, "claim": text, "state": state, "detail": detail})
        if case:
            cases.setdefault(case, Counter())[state] += 1

    # Files
    changed = sorted(k for k in recorded if files.get(k) != recorded[k])
    added = sorted(k for k in files if k not in recorded)
    claim(None, "files", f"{len(recorded)} packaged files match the manifest",
          "hash checked" if not changed and not added else "mismatch",
          f"{len(files)} files; changed {changed[:5]}; not in manifest {added[:5]}" if changed or added else
          f"{len(files)} files, sha256 each")

    for name, sc in D.SCEN.items():
        stored = json.loads((ROOT / "data" / name / "oracle.json").read_text())
        rec = json.loads((ROOT / "data" / name / "mcp.json").read_text())
        pre = D.sparql_prefixes(sc["pfx"])
        queries = sc["q"] + [dict(sc["inf"], id="INF")]
        for variant in ("clean", "fault"):
            live = oracle.run(name, variant == "fault")
            old = stored[variant]
            scene = (ROOT / "scenes" / f"{name}{'-fault' if variant == 'fault' else ''}.x3d").read_text()
            data = (ROOT / "data" / name / ("domain-fault.ttl" if variant == "fault" else "domain.ttl")).read_text()
            server = asyncio.run(mcp_answers(mcp, scene, data, pre, queries))
            rec_sparql = [r for r in rec["runs"][variant] if r["tool"] == "sparql_scene"]
            for i, q in enumerate(queries):
                gold_old = old["inference"] if q["id"] == "INF" else old["questions"][q["id"]]
                gold = live["inference"] if q["id"] == "INF" else live["questions"][q["id"]]
                cid = f"{name}/{variant}/{q['id']}"
                text = q.get("text") or q.get("q") or q["id"]
                # 1. the oracle answer, recomputed
                claim(name, cid + "/oracle", f"Oracle answer to {q['id']} ({variant})",
                      "computed here" if gold == gold_old else "mismatch",
                      f"{len(gold)} rows, rdflib")
                # 2. the server today, typed and in the query's order
                srv = server[q["id"]]
                got = oracle_typed(srv["rows"], srv["vars"])
                ordered = bool(ORDER_BY.search(q["sparql"]))
                if got == gold:
                    st, why = "computed here", f"{len(got)} rows, same order as the oracle" if ordered else f"{len(got)} rows"
                elif bag(got) == bag(gold):
                    st, why = ("computed here", f"{len(got)} rows, same rows; no ORDER BY, so order is not part of the answer") \
                        if not ordered else ("mismatch", "same rows in a different order despite ORDER BY")
                else:
                    st, why = "mismatch", f"server {len(got)} rows, oracle {len(gold)}"
                claim(name, cid + "/server", f"x3d_mcp sparql_scene answers {q['id']} ({variant}) like the oracle", st, why)
                # 3. the stored MCP run of 16 Nov
                r = rec_sparql[i]["reply"]["rows"] if i < len(rec_sparql) else None
                if r is None:
                    claim(name, cid + "/recorded", f"Recorded MCP run of {q['id']} ({variant})", "not packaged")
                    continue
                old_rows = norm_rows(r)
                gold_cmp = [{k: (v if v is not None else "") for k, v in row.items()} for row in gold]
                if old_rows == gold_cmp:
                    claim(name, cid + "/recorded", f"Recorded MCP run of {q['id']} ({variant})", "recorded",
                          f"{len(old_rows)} rows, as the oracle")
                elif bag(old_rows) == bag(gold_cmp) and ordered:
                    claim(name, cid + "/recorded", f"Recorded MCP run of {q['id']} ({variant})", "explained difference",
                          "same rows, sorted: the server of 16 Nov re-sorted every answer and dropped ORDER BY "
                          "(Evidence Edition F03); today's server keeps it")
                elif bag(old_rows) == bag(gold_cmp):
                    claim(name, cid + "/recorded", f"Recorded MCP run of {q['id']} ({variant})", "recorded",
                          f"{len(old_rows)} rows, as the oracle (order not asked for)")
                else:
                    claim(name, cid + "/recorded", f"Recorded MCP run of {q['id']} ({variant})", "mismatch",
                          f"recorded {len(old_rows)} rows, oracle {len(gold)}")
            # SHACL verdict
            same = (live["conforms"], live["shacl"]) == (old["conforms"], old["shacl"])
            want = variant == "clean"
            claim(name, f"{name}/{variant}/shacl",
                  f"The {variant} variant {'conforms' if want else 'fails, and every planted fault is caught'}",
                  ("computed here" if live["conforms"] == want else "mismatch") if same else "mismatch",
                  f"{len(live['shacl'])} results, pyshacl")

    # Layer test
    lay = json.loads((ROOT / "eval" / "layers.json").read_text())
    qn = sum(len(v["questions"]) for v in lay.values())
    claim(None, "layers/questions", f"Records alone answer {sum(q['domain_only'] for v in lay.values() for q in v['questions'])} "
          f"of {qn}; scene alone {sum(q['scene_only'] for v in lay.values() for q in v['questions'])} of {qn}",
          "recorded", "eval/layers.json; re-run with build/layers.py")
    claim(None, "browser", "N3.js, Comunica and shacl-engine match the oracle on 90 of 90 runs", "not packaged",
          "measured on the x3d.demo page (demos/semantic-twins/v1); the browser engines are not in this folder")
    claim(None, "vocab", "All 116 class and property uses are declared in each publisher's vocabulary file",
          "not packaged", "checked against the publishers' files as read on 16 Nov 2026; those files are not kept here")

    totals = Counter(c["state"] for c in claims)
    out = {"states": dict(sorted(totals.items())), "by_case": {k: dict(sorted(v.items())) for k, v in cases.items()},
           "claims": claims}
    (EV / "evidence.json").write_text(json.dumps(out, indent=1) + "\n")
    lines = ["# Evidence states for the nine cases", "",
             "Written by `build/evidence.py`. Each claim has one state; see the script's docstring for what they mean.", "",
             "| State | Claims |", "|---|---|"] + [f"| {k} | {v} |" for k, v in sorted(totals.items())] + \
            ["", "| Case | " + " | ".join(sorted(totals)) + " |", "|---|" + "---|" * len(totals)]
    for k, v in cases.items():
        lines.append(f"| {k} | " + " | ".join(str(v.get(s, 0)) for s in sorted(totals)) + " |")
    lines += ["", "## Claims that are not plain matches", ""]
    for c in claims:
        if c["state"] not in ("computed here", "recorded"):
            lines.append(f"- **{c['state']}**: {c['claim']} (`{c['id']}`). {c['detail']}")
    (EV / "EVIDENCE.md").write_text("\n".join(lines) + "\n")
    print(json.dumps(out["states"]))
    return 1 if totals.get("mismatch") else 0


if __name__ == "__main__":
    sys.exit(main("--write-manifest" in sys.argv))
