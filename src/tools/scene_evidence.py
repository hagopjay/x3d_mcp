"""scene_evidence: check the quotes behind a scene's values.

Read-only; no model is called. A writer (a model, a tool, a person) lists claims {field, value, quote, source?}; plain code
FINDs each quote in the source, COMPAREs the value with the quote, LOCATEs it, and BINDs the value to the scene's own
attribute. The answer carries verdict words, offsets and handles, not the quotes (text="quoted" adds a capped sample).
"""

import json

from mcp.server.fastmcp import Context, FastMCP

from tools.granular import _scene_for
from transfer.errors import BlobError
from transfer.outputs import clip, document_info, envelope, error_envelope, parse_mode
from transfer.tooling import holds_input, require_blob_access, resolve_input, run_heavy
from x3d_utils import scene_evidence as ev


def _fail(code: str, mode: str = "full", **extra) -> str:
    if mode == "full":
        return json.dumps({"schema": ev.SCHEMA, "ok": False, "error": code, **extra}, sort_keys=True, separators=(",", ":"))
    return error_envelope(mode, code)


def register(mcp: FastMCP):

    @mcp.tool(structured_output=False)
    @holds_input
    async def scene_evidence(claims: str = "", content: str = "", path: str = "", blob_id: str = "",
                             current_scene: bool = False, sources: str = "", claims_blob_id: str = "",
                             expect: str = "", policy: str = "default", bind: bool = True,
                             text: str = "counts", text_cap: int = 80, max_items: int = 200,
                             expect_report_sha256: str = "", return_mode: str = "full",
                             ctx: Context = None) -> str:
        """Check the quotes behind a scene's values: is the quote in the source, is the value in the quote, is it the scene's value.

        A writer lists one claim per value: {"field","value","quote","source"?,"prefix"?,"suffix"?}. `field` is
        "DEF@attr", "Tag[DEF].attr", "n<k>@attr" (a scene_brief handle) or the inventory path. `sources` is a JSON list of
        {"id","text"} (or {"id","blob_id"}); leave it empty to check quotes taken from the scene itself. Verdicts:
        verified (quote found, value inside it), reworded (value is a close reading), check (not found or value not in the
        quote), unchecked (no quote). `bind` says whether the scene holds the claimed value at that field. "verified" means
        the quote contains the value, not that the quote is the right place; a repeated quote is resolved only by
        prefix/suffix. The answer never repeats a quote or value unless text="quoted"; items carry offsets, line/col and handles.

        Args:
            claims: JSON list of claims (or {"claims": [...]}), or use claims_blob_id.
            content: X3D XML text of the scene. Provide exactly one of content, path, blob_id or current_scene.
            path: An X3D file (stdio only).
            blob_id: A stored blob holding the scene.
            current_scene: Use the granular (in-memory) scene.
            sources: JSON list of {"id","text"|"blob_id","kind"?} the writer read. The scene is always source "scene".
            claims_blob_id: A stored blob holding the claims JSON, instead of `claims`.
            expect: "" (no coverage), "inventory" (every value-bearing attribute of the scene), or a comma list of fields.
            policy: "default", "strict" or a JSON object of TracePolicy fields (see docs/scene-evidence.md).
            bind: Check that the scene holds the claimed value at `field` (default true).
            text: "counts" (default: digests and lengths only), "off", or "quoted" (capped, JSON-escaped quotes under untrusted).
            text_cap: Characters per quoted string (16 to 200).
            max_items: Items returned (the totals always cover every claim).
            expect_report_sha256: A report_sha256 from an earlier answer; the answer says whether it still recomputes to it.
            return_mode: "full" (default), "summary" (counts and the items needing attention) or "blob" (stores the report).

        Returns JSON {schema, ok, engine, scene, sources, summary{verified,check,reworded,unchecked,total,bound,unbound,
        coverage?,verified_coverage?}, items[{i,verdict,status,bind,grade_cap,span,at}], claims_sha256, report_sha256, policy}.
        grade_cap is the highest claim_append grade the evidence allows (verified: Confirmed, reworded or unchecked: Likely, check: Suspected).
        """
        try:
            mode = parse_mode(return_mode)
        except ValueError as e:
            return error_envelope("full", str(e))
        try:
            if current_scene:
                if any((v or "").strip() for v in (content, path, blob_id)):
                    raise ValueError("Provide exactly one of `content`, `path`, `blob_id` or `current_scene`.")
                xml, label = _scene_for(ctx).to_xml(), "current scene"
            else:
                xml, label = await resolve_input(content, path, blob_id, ctx)
            if (claims_blob_id or "").strip():
                if (claims or "").strip():
                    raise ValueError("Provide `claims` or `claims_blob_id`, not both.")
                claims_text, _ = await resolve_input("", "", claims_blob_id, ctx)
            else:
                claims_text = claims
            items = []
            if (sources or "").strip():
                try:
                    items = json.loads(sources)
                except json.JSONDecodeError:
                    return _fail("sources-not-json", mode)
                if not isinstance(items, list) or not all(isinstance(x, dict) for x in items):
                    return _fail("sources-not-a-list", mode)
                for it in items:
                    if (it.get("blob_id") or "").strip():
                        it["text"], _ = await resolve_input("", "", it.pop("blob_id"), ctx)
            if mode == "blob":
                require_blob_access(ctx)
            out = await run_heavy(_run, xml, claims_text, items, policy, expect, bind, text, int(text_cap),
                                  max(1, min(int(max_items), 2000)), expect_report_sha256, size=len(xml) + len(claims_text))
        except ev.EvidenceError as e:
            return _fail(e.code, mode, **e.extra)
        except (ValueError, BlobError) as e:
            return error_envelope(mode, clip(str(e), 300))
        if mode == "full":
            return ev.render(out)
        if mode == "summary":
            return envelope(mode, label, **ev.card(out))
        try:
            info = document_info(ev.render(out), "application/json", True, "scene-evidence.json")
        except BlobError as e:
            return error_envelope(mode, str(e), label)
        return envelope(mode, label, **ev.card(out), report_blob_id=info["blob_id"], report_blob_bytes=info["bytes"])


def _run(xml, claims_text, source_items, policy, expect, bind, text, text_cap, max_items, expect_sha):
    return ev.scene_evidence(xml, ev.parse_claims(claims_text), source_items, policy=ev.parse_policy(policy), expect=expect,
                             bind=bind, text_mode=text, text_cap=min(200, max(16, text_cap)), max_items=max_items,
                             expect_report_sha256=expect_sha)
