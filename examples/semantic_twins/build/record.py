"""Recorded MCP evidence: start the real x3d_mcp server over stdio (stock src/server.py, branch with data_ttl) and make
the calls an LLM would make for each scenario. Writes data/<s>/mcp.json with arguments (big texts as sha256) and replies."""
import asyncio, hashlib, json, sys, time
from pathlib import Path
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "build"))
import cases as D

SRC = Path(__file__).resolve().parents[3] / "src"
SERVER = str(SRC / "server.py")
sha = lambda s: hashlib.sha256(s.encode()).hexdigest()


def logged(args):
    return {k: (f"<{len(v):,} chars, sha256 {sha(v)[:16]}>" if isinstance(v, str) and len(v) > 300 else v) for k, v in args.items()}


async def main():
    params = StdioServerParameters(command=sys.executable, args=[SERVER], cwd=str(SRC))
    async with stdio_client(params) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = {t.name for t in (await s.list_tools()).tools}

            async def call(log, name, args, keep=None):
                t0 = time.perf_counter()
                res = await s.call_tool(name, args)
                ms = round((time.perf_counter() - t0) * 1000, 1)
                text = res.content[0].text
                out = json.loads(text)
                if keep:
                    out = keep(out)
                log.append({"tool": name, "args": logged(args), "wall_ms": ms, "reply_sha256": sha(text), "reply": out})
                return json.loads(text)

            for name, sc in D.SCEN.items():
                rec = {"server": "<checkout>/src/server.py", "tools": len(tools), "runs": {}}
                pre = D.sparql_prefixes(sc["pfx"])
                for variant in ("clean", "fault"):
                    log = []
                    scene = (ROOT / "scenes" / f"{name}{'-fault' if variant == 'fault' else ''}.x3d").read_text()
                    data = (ROOT / "data" / name / ("domain-fault.ttl" if variant == "fault" else "domain.ttl")).read_text()
                    conv = await call(log, "scene_to_rdf", {"content": scene, "return_mode": "summary"},
                                      keep=lambda o: {k: o.get(k) for k in ("ok", "graph_id", "triples", "individuals", "base", "warnings")})
                    gid = conv["graph_id"]
                    for q in sc["q"] + [dict(sc["inf"], id="INF")]:
                        await call(log, "sparql_scene", {"sparql": pre + q["sparql"], "graph_id": gid, "data_ttl": data, "include_ontology": False},
                                   keep=lambda o: {k: o.get(k) for k in ("ok", "total", "rows", "error", "ms")})
                    await call(log, "shacl_validate", {"graph_id": gid, "shapes": "none", "extra_shapes_ttl": sc["shapes"], "data_ttl": data,
                                                       "return_mode": "full"},
                               keep=lambda o: {k: o.get(k) for k in ("ok", "conforms", "counts", "results", "error", "ms")})
                    rec["runs"][variant] = log
                (ROOT / "data" / name / "mcp.json").write_text(json.dumps(rec, indent=1))
                print(name, "ok")


asyncio.run(main())
