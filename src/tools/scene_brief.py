"""scene_brief: a small, budgeted, injection-sliced description of a scene (x3d_utils/scene_brief.py has the rules).

Read-only. It answers what a model needs to decide where to look next (how big, what is in it, where the camera is, what
the validators say) in a size the caller sets, without handing it the document or any text the scene's author wrote.
"""

import json

from mcp.server.fastmcp import Context, FastMCP

from tools.granular import _scene_for
from transfer.errors import BlobError
from transfer.outputs import clip, document_info, envelope, error_envelope, parse_mode
from transfer.tooling import holds_input, require_blob_access, resolve_input, run_heavy
from x3d_utils import scene_brief as brief


def _fail(code: str, mode: str = "full", **extra) -> str:
    if mode == "full":
        return json.dumps({"schema": brief.SCHEMA, "ok": False, "error": code, **extra}, sort_keys=True, separators=(",", ":"))
    return error_envelope(mode, code)


def register(mcp: FastMCP):

    @mcp.tool(structured_output=False)
    @holds_input
    async def scene_brief(content: str = "", path: str = "", blob_id: str = "", current_scene: bool = False,
                          facets: str = "", budget_bytes: int = brief.DEFAULT_BUDGET, max_items: int = 8,
                          text: str = "counts", text_cap: int = 80, ids: str = "handles",
                          return_mode: str = "full", ctx: Context = None) -> str:
        """A small, trust-tiered description of an X3D scene, sized to a byte budget (content, path, blob OR the current scene).

        Use it to decide where to look before spending a large answer: how many nodes and of what kinds, the bounding box
        of the authored pose, the first viewpoint, lights, materials, ROUTE counts and the XSD and semantic verdicts. It is
        a slice, not a proof. The scene author's text (DEF names, meta, titles, descriptions, urls, Script source,
        comments) never appears in a facet; it is counted and digested under `untrusted`, and quoted there only when
        text="quoted". Validator messages are not forwarded: XSD errors become a fixed `kind`, semantic findings keep
        their check id. Handles `n<k>` are the 1-based document-order index of an element of the Scene (stable for one
        input.sha256_full, shifted by an insertion). The legacy input.sha256 is a 16-hex display prefix;
        use input.sha256_full plus the handle when recording source identity.

        Do not give the `validators` facet to a judge you are scoring against these same validators: ask for the other
        facets (facets="census,bounds,camera,lights,materials,routes,text").

        Args:
            content: X3D XML text. Provide exactly one of content, path, blob_id or current_scene.
            path: An X3D file (stdio only).
            blob_id: A stored blob holding the document.
            current_scene: Brief the granular (in-memory) scene built with create_node/add_child.
            facets: Comma-separated subset of census,bounds,camera,lights,materials,routes,validators,text (empty = all).
            budget_bytes: Hard cap on the whole answer in UTF-8 bytes (1024 to 65536; default 4096, clamped and reported).
            max_items: List entries per facet (1 to 50); the budget may lower it.
            text: "counts" (default, text counted and digested), "off", or "quoted" (adds capped, JSON-escaped strings).
            text_cap: Characters per quoted string (16 to 200).
            ids: "handles" (default) or "names" (identifier-shaped DEF names appear under untrusted.names only).
            return_mode: "full" (default), "summary" (a verdict card), or "blob" (stores the brief, returns its blob_id).

        Returns JSON {schema, ok, input, facets, untrusted, budget{bytes,used,dropped}, brief_sha256, timing_ms}.
        `budget.used` is an upper bound on the answer's length; `budget.dropped` names every step taken to fit.
        """
        try:
            mode = parse_mode(return_mode)
        except ValueError as e:
            return error_envelope("full", str(e))
        try:
            if current_scene:
                if any((v or "").strip() for v in (content, path, blob_id)):
                    raise ValueError("Provide exactly one of `content`, `path`, `blob_id` or `current_scene`.")
                xml, source = _scene_for(ctx).to_xml(), "current scene"
            else:
                xml, source = await resolve_input(content, path, blob_id, ctx)
            want = [f.strip() for f in (facets or "").split(",") if f.strip()]
            unknown = [f for f in want if f not in brief.FACETS]
            if unknown:
                return _fail("unknown-facet", mode, allowed=list(brief.FACETS))
            if mode == "blob":
                require_blob_access(ctx)
            out = await run_heavy(brief.scene_brief, xml, size=len(xml), facets=want or None, budget_bytes=int(budget_bytes),
                                  max_items=int(max_items), text_mode=text, text_cap=int(text_cap), ids=ids)
        except brief.BriefError as e:
            return _fail(e.code, mode, **({"line": e.line} if e.line else {}), **e.extra)
        except (ValueError, BlobError) as e:
            return error_envelope(mode, clip(str(e), 300))
        if mode == "full":
            return brief.render(out)
        if mode == "summary":
            return envelope(mode, source, **brief.card(out))
        try:
            info = document_info(brief.render(out), "application/json", True, "scene-brief.json")
        except BlobError as e:
            return error_envelope(mode, str(e), source)
        return envelope(mode, source, **brief.card(out), brief_blob_id=info["blob_id"], brief_blob_bytes=info["bytes"])
