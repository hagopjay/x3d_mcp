"""X3D MCP Server.

FastMCP server providing tools for generating, validating, and converting
valid X3D 4.1 content.

Transport is selected by the MCP_TRANSPORT environment variable:
  - "stdio" (default): launched as a subprocess by a desktop MCP client.
  - "streamable-http" (or "http"): listens for remote MCP clients over
    HTTP. Host and port come from HOST and PORT (PORT is injected by
    hosts like Render). Endpoint path is /mcp.
"""

import os
import sys
from pathlib import Path

# Ensure src is on the path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent))

from mcp.server.fastmcp import FastMCP
from starlette.responses import JSONResponse

from transfer import blobs as _blobs
from transfer.auth import explicit_security
from transfer.errors import ConfigError
from transfer.limits import SplatLimits

_TRANSPORT = os.environ.get("MCP_TRANSPORT", "stdio").lower()
_IS_HTTP = _TRANSPORT in ("http", "streamable-http")

# Blob store settings come from X3D_MCP_BLOB* variables; a bad value, or a spill directory
# that is not private, not ours or in use by another process, stops the server here.
try:
    _blob_cfg = _blobs.configure()
    _blobs.start()
    _host_security = explicit_security(_blob_cfg) if _IS_HTTP else None
    SplatLimits.from_env()          # X3D_MCP_SPLAT_* are checked here too
except ConfigError as _exc:
    raise SystemExit(f"x3d-mcp: {_exc}")

_real_stdout = sys.stdout
sys.stdout = sys.stderr
from tools import (
    workflow, granular, convert, query, validate_tool, render, scene_ops, animate,
    claims as claim_tools, prompts, semantics, semantic_web, scene_brief as brief_tools,
    scene_evidence as evidence_tools,
)
from tools import transfer as blob_tools
from tools import splats as splat_tools
sys.stdout = _real_stdout

# Surfaced to the client on connect -- the proactive "read me first" that keeps a
# fresh model on X3D's canonical path instead of improvising from the flat node list.
INSTRUCTIONS = """\
X3D is authored ONE canonical way. Follow the path below every time; do not improvise \
field names, containerFields, or node placement from memory -- this server is the ground truth.

THE PATH
1. LOOK UP before you build. `describe_node('<Name>')` gives a node's exact fields, types, \
default containerField, and which child node types each field accepts. `list_nodes(component=...)` \
discovers nodes. Never guess field or containerField names.
2. BUILD (workflow tools: create_scene / create_geometry / compose_scene; or granular: \
create_node / set_field / add_child / add_route).
3. VALIDATE -- always BOTH: `validate_x3d` (XSD schema) and `validate_semantic` (the errors that \
pass the schema but still break rendering -- wrong containerField, USE-before-DEF, broken ROUTEs). \
validate_semantic names the exact fix; `repair_x3d` applies all of them (containerField, profile, \
version, component statements) and returns the repaired document with each change explained.
4. RENDER and LOOK before declaring done: `render_image(content|path)` returns a PNG you \
inspect directly; `x3dom_page` gives an interactive browser page. A scene that validates can \
still be visually wrong (off-camera, unlit, mis-scaled).
Rule: a scene is not finished until validate_semantic is clean AND you have rendered it.

For any multi-step task, START from a prompt (build_scene, animate_scene, audit_scene, \
semantic_audit, convert_to_x3dom). They encode the known-good tool order so you don't have to rediscover it.

MEANING, NOT ONLY SYNTAX (the semantic_audit prompt runs this): `scene_to_rdf` turns a scene into RDF \
(X3D Ontology 4.1 terms, the Consortium X3dToTurtle conventions) and returns a `graph_id`; \
`sparql_scene(graph_id=..., sparql=...)` answers questions about it (which Shapes lack an Appearance, \
what a ROUTE chain drives, every geometry via `a/rdfs:subClassOf*`); `shacl_validate(graph_id=...)` \
checks it against shapes and names each offending node, its DEF, the check and a fix; then \
`validate_x3d` + `validate_semantic` for schema and order (RDF keeps no document order); record what \
you verified with `claim_append`, passing the `instrument` object these tools return. `rdf_infer` shows \
what reasoning adds (subclass-only by default); `ontology_vs_gate(child)` compares the ontology's map of \
where a node may go with the X3DUOM gate (`where_can_place`) and the XSD. The ontology proposes, the \
shapes dispose, the validators gate: an ontology answer is never a validity verdict.

NON-OBVIOUS RULES THAT CAUSE SILENT FAILURES (validate_semantic enforces all of these):
- containerField: every child node belongs to a specific *field* of its parent. A node's DEFAULT \
containerField only fits its usual parent; when you place it elsewhere you MUST set containerField \
explicitly -- e.g. a texture inside PhysicalMaterial needs baseTexture/normalTexture/..., an \
HAnimHumanoid skeleton root needs containerField='skeleton', skinCoord/skinNormal likewise. A wrong \
or defaulted containerField makes a conforming viewer silently drop or misfile the node (blank render). \
`autofix_x3d` rewrites these for you and returns the corrected document.
- DEF before USE: a USE must appear AFTER the DEF it references, in document order.
- ROUTE field types and access types must match source->target.

SMALL ANSWERS ABOUT A BIG SCENE: `scene_brief(content|path|blob_id, budget_bytes=...)` returns a trust-tiered \
description of a scene in a size you set (nodes, bounds, camera, lights, ROUTE counts, validator verdicts) and \
never contains text the scene's author wrote; it is a slice, not a proof, so still validate and render.

EVIDENCE FOR VALUES: `scene_evidence(content|path|blob_id, claims=[{field, value, quote}], sources=[{id, text}])` checks that each quote is in \
its source, that the value is in the quote and that the scene holds that value; it returns verdicts, offsets and handles, never \
the quotes. "verified" means the quote contains the value, not that it is the right place.

When unsure, the answer is describe_node + validate_semantic -- not recall."""

# Only when the blob store is on: tells the model how to keep big documents out of its context.
BLOB_INSTRUCTIONS = """

LARGE SCENES, SPLATS AND DATA FILES: never paste a document over about 100 KB into a tool call, \
and do not ask for one back. Store it once, then name it. Under stdio pass `path=`; over HTTP \
the harness uploads with `PUT /blob` (bearer token) and small or compressed payloads go through \
`blob_put`. Then pass `blob_id=<id>` to validate_x3d, validate_semantic, autofix_x3d, repair_x3d, \
convert_x3d, modify_x3d_node, remove_x3d_node and move_x3d_node (leave `content` empty where it is \
required). Add `return_mode="summary"` to get the digest, size, node counts and the list of \
changes instead of the document, or `return_mode="blob"` to keep the result server-side and get \
its new blob_id for the next call. `blob_info` and `blob_read` (paged) look inside a stored blob \
when you must see part of it."""
if _blob_cfg.enabled:
    INSTRUCTIONS += BLOB_INSTRUCTIONS

# Gaussian splat files are too big for any tool call: they are named, inspected and cut on the server.
# Registered when numpy is installed, and over HTTP only with the blob store (a hosted client has no path).
_SPLATS = splat_tools.numpy_available() and (_blob_cfg.enabled or not _IS_HTTP)
SPLAT_INSTRUCTIONS = """

GAUSSIAN SPLATS: a splat scene is 5 to 65 MB and one GaussianSplats node cannot be validated past roughly \
75,000 to 100,000 splats (libxml2's 10 MB start-tag limit). Do not paste splat data. Name the file (`blob_id=`, or \
`path=` under stdio) and call `splat_inspect` for its facts and a verdict, then `splat_chunk` to cut it \
into small valid X3D files plus a master scene of Inline nodes and a manifest; the answer lists blob ids, \
never contents. validate_x3d on an oversize scene answers "not-checkable-at-this-size" (not a parse error)."""
if _SPLATS:
    INSTRUCTIONS += SPLAT_INSTRUCTIONS

_fastmcp_kwargs = {}
if _IS_HTTP:
    _fastmcp_kwargs["host"] = os.environ.get("HOST", "0.0.0.0")
    _fastmcp_kwargs["port"] = int(os.environ.get("PORT", "8000"))
    if _host_security is not None:
        # X3D_MCP_ALLOWED_HOSTS / _ORIGINS turn on the SDK's DNS-rebinding checks for /mcp
        # (and /blob); without them a hosted bind has none, as before.
        _fastmcp_kwargs["transport_security"] = _host_security

mcp = FastMCP("x3d-mcp", instructions=INSTRUCTIONS, **_fastmcp_kwargs)

workflow.register(mcp)
granular.register(mcp)
convert.register(mcp)
query.register(mcp)
validate_tool.register(mcp)
render.register(mcp)
scene_ops.register(mcp)
animate.register(mcp)
prompts.register(mcp)
semantics.register(mcp)
semantic_web.register(mcp)
claim_tools.register(mcp)
brief_tools.register(mcp)
evidence_tools.register(mcp)

if _SPLATS:
    splat_tools.register(mcp)

if _blob_cfg.enabled:
    blob_tools.register(mcp)
    if _IS_HTTP:
        # PUT/POST/GET/HEAD/DELETE /blob: exist only with X3D_MCP_BLOB_TOKEN set.
        from transfer import routes as _blob_routes
        _blob_routes.register(mcp, _blob_cfg)


# Friendly responses on the root and /pulse paths under HTTP transport.
# These are no-ops under stdio. Without them, browser and platform hits
# to "/" return a 404 from FastMCP's app -- correct for the MCP protocol
# but unfriendly to humans and to host platforms probing for liveness.
# /pulse is intended as the configured health-check endpoint on hosts
# like Render and is kept dependency-free so it stays fast and reliable.
@mcp.custom_route("/", methods=["GET"])
async def _root(_request):
    return JSONResponse(
        {
            "name": "x3d-mcp",
            "description": "Web3D Consortium MCP server for X3D 4.0/4.1 authoring.",
            "mcp_endpoint": mcp.settings.streamable_http_path,
            "health_endpoint": "/pulse",
            "repository": "https://github.com/Web3DConsortium/x3d_mcp",
        }
    )


@mcp.custom_route("/pulse", methods=["GET"])
async def _pulse(_request):
    return JSONResponse({"status": "ok"})


if __name__ == "__main__":
    if _IS_HTTP:
        print(
            f"x3d-mcp listening on http://{mcp.settings.host}:{mcp.settings.port}"
            f"{mcp.settings.streamable_http_path} (streamable-http)",
            file=sys.stderr,
        )
        if _blob_cfg.enabled:
            print("x3d-mcp: blob routes enabled at /blob (bearer token required)", file=sys.stderr)
            if not (_blob_cfg.allowed_hosts or _blob_cfg.allowed_origins):
                print("x3d-mcp: warning: X3D_MCP_ALLOWED_HOSTS/_ORIGINS are not set; the bearer "
                      "token alone protects /blob", file=sys.stderr)
        mcp.run(transport="streamable-http")
    else:
        if _blob_cfg.enabled and _blob_cfg.auto_spill:
            _blobs.exit_on_sigterm()          # hosts stop stdio servers with SIGTERM
        mcp.run()
