"""X3D validation MCP tools.

Exposes validation pipeline as MCP tools.
"""

import json
import re
from collections import Counter
from mcp.server.fastmcp import FastMCP

from validation.validate import validate_xml, validate_json
from validation.semantic import validate_semantic as _validate_semantic
from mcp.server.fastmcp import Context
from validation.autofix import autofix_containerfields
from validation.repair import repair_x3d as _repair_x3d
from tools.granular import _scene_for
from transfer.errors import BlobError
from transfer.outputs import (
    XML_TYPE, cap_items, clip, clip_deep, document_info, envelope, error_envelope, input_info,
    parse_mode, report_info,
)
from transfer.tooling import holds_input, require_blob_access, resolve_input, run_heavy


_SEM_ITEM = re.compile(r"- \*\*\[([^\]]+)\]\*\* (.*)")
_SEM_LEVELS = {"## Errors": "error", "## Warnings": "warning", "## Info": "info"}


def _semantic_summary(report: str) -> dict:
    """Counts and the first findings of a validate_semantic markdown report."""
    head = report.split("\n", 1)[0]
    if head.startswith(("# Semantic Check: Parse Error", "# Semantic Check: No Scene")):
        return {"ok": False, "error": clip(report.split("\n\n", 1)[-1])}
    findings, level = [], None
    for line in report.splitlines():
        if line in _SEM_LEVELS:
            level = _SEM_LEVELS[line]
            continue
        m = _SEM_ITEM.fullmatch(line)
        if m and level:
            findings.append({"level": level, "check": m.group(1), "message": clip(m.group(2))})
    by_level = Counter(f["level"] for f in findings)
    return {
        "ok": True,
        "errors": by_level["error"], "warnings": by_level["warning"], "infos": by_level["info"],
        "by_check": dict(Counter(f["check"] for f in findings).most_common()),
        **cap_items(findings, "findings"),
    }


def register(mcp: FastMCP):

    @mcp.tool()
    @holds_input
    async def validate_x3d(content: str = "", encoding: str = "xml", path: str = "",
                           blob_id: str = "", return_mode: str = "full",
                           ctx: Context = None) -> str:
        """Validate X3D against the XSD schema for its declared version (inline content, a file path OR a stored blob).

        XML is checked against the schema matching the document's `version`
        attribute: 4.0 (ratified) or 4.1 (draft, the default when no version is
        given). A node that exists only in a later version (EnvironmentLight,
        GaussianSplats, HAnimPose, ...) is named in `notes` with the version
        that has it. JSON is checked against the X3D 4.0 JSON Schema.

        Args:
            content: The X3D content string to validate (inline).
            encoding: Content encoding - "xml" or "json".
            path: Path to an X3D file to validate instead of inline content.
                  Provide exactly one of `content`, `path` or `blob_id`.
            blob_id: Id of a stored blob holding the document (a large scene is uploaded
                  once, then named here instead of pasted).
            return_mode: "full" (default) returns the whole report. "summary" returns
                  JSON: valid, error count, the first errors (clipped) and the input digest.
                  "blob" also stores the full report and returns its blob_id.
        """
        try:
            mode = parse_mode(return_mode)
            text, label = await resolve_input(content, path, blob_id, ctx)
            if mode == "blob":
                require_blob_access(ctx)
        except ValueError as exc:
            return json.dumps({"valid": False, "errors": [str(exc)]}, indent=2)
        if encoding == "xml":
            result = await run_heavy(validate_xml, text, size=len(text))
        elif encoding == "json":
            result = await run_heavy(validate_json, text, size=len(text))
        else:
            result = {"valid": False, "errors": [f"Unsupported encoding: {encoding}"]}
        if mode == "full":
            return json.dumps(result, indent=2)
        body = {"valid": result["valid"], "schema_version": result.get("schema_version"),
                "error_count": len(result.get("errors", [])),
                **cap_items([clip(e) for e in result.get("errors", [])], "errors"),
                **cap_items([clip(n) for n in result.get("notes", [])], "notes")}
        for key in ("checked", "verdict"):          # set when libxml2's size limits stopped the check (validation.oversize)
            if key in result:
                body[key] = result[key]
        if mode == "blob":
            body["report"] = report_info(json.dumps(result, indent=2), "application/json")
        inp = await run_heavy(input_info, text, blob_id, size=len(text))
        return envelope(mode, label, input=inp, **body)

    @mcp.tool()
    def validate_current_scene(ctx: Context = None) -> str:
        """Validate the current granular (in-memory) scene -- BOTH schema and semantic.

        Runs the XSD check for the scene's declared version and the semantic checks (containerField, USE-order,
        ROUTEs, Shape completeness, ...) on the scene built via create_node/add_child,
        so granular-mode authoring gets the same safety net as content-based tools.
        """
        xml_content = _scene_for(ctx).to_xml()
        schema = validate_xml(xml_content)
        semantic = _validate_semantic(xml_content)
        return (
            "## Schema (XSD)\n```json\n"
            + json.dumps(schema, indent=2)
            + "\n```\n\n"
            + semantic
        )

    @mcp.tool()
    @holds_input
    async def validate_semantic(content: str = "", path: str = "", blob_id: str = "",
                                return_mode: str = "full", ctx: Context = None) -> str:
        """Run semantic checks on X3D beyond XSD schema validation (content, path OR a stored blob).

        Detects authoring issues XSD cannot catch and that silently break rendering:
        nodes outside the declared profile and component statements (X_ITE refuses
        such scenes; the fix lists the component statements to add),
        wrong/defaulted containerField (with the correct field suggested),
        USE-before-DEF ordering, ROUTE field/type/access-type problems, Shapes
        without geometry, empty grouping nodes, duplicate DEFs, and missing
        Viewpoints. Returns a markdown report.

        Args:
            content: The X3D XML content string to check (inline).
            path: Path to an X3D file to check instead of inline content.
                  Provide exactly one of `content`, `path` or `blob_id`.
            blob_id: Id of a stored blob holding the document.
            return_mode: "full" (default) returns the markdown report. "summary" returns
                  JSON: error, warning and info counts, counts per check, and the first
                  findings. "blob" also stores the full report and returns its blob_id.
        """
        try:
            mode = parse_mode(return_mode)
            text, label = await resolve_input(content, path, blob_id, ctx)
            if mode == "blob":
                require_blob_access(ctx)
        except ValueError as exc:
            return f"# Semantic Check: Input Error\n\n{exc}"
        report = await run_heavy(_validate_semantic, text, size=len(text))
        if mode == "full":
            return report
        body = _semantic_summary(report)
        if mode == "blob":
            body["report"] = report_info(report, "text/markdown")
        inp = await run_heavy(input_info, text, blob_id, size=len(text))
        return envelope(mode, label, input=inp, **body)

    @mcp.tool()
    @holds_input
    async def autofix_x3d(content: str = "", path: str = "", blob_id: str = "",
                          return_mode: str = "full", ctx: Context = None) -> str:
        """Auto-correct containerField mistakes and return the FIXED X3D document.

        The companion to validate_semantic's containerField check: instead of only
        naming the right field, it rewrites it -- e.g. a texture defaulted to
        'texture' under a PhysicalMaterial becomes containerField='baseTexture', and
        an HAnimHumanoid skeleton root becomes containerField='skeleton'. Only
        containerField attributes are touched; no node is moved, added, or removed.

        Returns JSON {fixed, changes, unfixable}. `fixed` is the corrected document
        -- use it directly. Each change flags `ambiguous` (several fields accept the
        node) with the `alternatives`, so override the chosen slot if needed.
        `unfixable` lists nodes whose parent accepts no such type (a real misplacement,
        not just a label). Does NOT reorder USE-before-DEF (that's structural).

        Args:
            content: The X3D XML content string to fix (inline).
            path: Path to an X3D file to fix instead of inline content.
                  Provide exactly one of `content`, `path` or `blob_id`.
            blob_id: Id of a stored blob holding the document.
            return_mode: "full" (default) returns {fixed, changes, unfixable}. "summary"
                  returns JSON without `fixed`: the fixed document's sha256, size and node
                  counts, plus changes and unfixable. "blob" also stores the fixed document
                  and returns its blob_id.
        """
        try:
            mode = parse_mode(return_mode)
            text, label = await resolve_input(content, path, blob_id, ctx)
            if mode == "blob":
                require_blob_access(ctx)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        result = await run_heavy(autofix_containerfields, text, size=len(text))
        if mode == "full":
            return json.dumps(result, indent=2)
        if result.get("error"):
            return error_envelope(mode, result["error"], label)
        try:
            doc = await run_heavy(document_info, result["fixed"], XML_TYPE, mode == "blob",
                                  size=len(result["fixed"]))
        except BlobError as exc:
            return error_envelope(mode, f"the result could not be stored: {exc}", label)
        inp = await run_heavy(input_info, text, blob_id, size=len(text))
        return envelope(mode, label, ok=True, input=inp, document=doc,
                        **cap_items(clip_deep(result["changes"]), "changes"),
                        **cap_items(clip_deep(result["unfixable"]), "unfixable"))

    @mcp.tool()
    @holds_input
    async def repair_x3d(content: str = "", path: str = "", blob_id: str = "",
                         return_mode: str = "full", ctx: Context = None) -> str:
        """Apply the fixes validate_x3d and validate_semantic name, and return the FIXED document.

        One call instead of a read-diagnose-edit loop. In order: containerField
        corrections (as autofix_x3d); a missing profile or version; a version raised
        to the lowest X3D version that has every node used (a 4.0 document using
        EnvironmentLight becomes 4.1 -- content is never removed); a DEF moved
        ahead of the sibling node whose USE precedes it; the <component>
        statements the profile check asks for, added to <head> or raised in level;
        the DOCTYPE and noNamespaceSchemaLocation updated to match; the https
        XMLSchema-instance namespace x3d.py writes replaced by the correct one.

        Returns JSON {fixed, changes, unfixable, remaining}. `changes` lists each
        edit with its kind (containerField, def-order, profile, version, component,
        xsi, doctype), old and new value, and reason -- read it and revert any edit
        you disagree with. `unfixable` names what needs a human decision (a USE
        before its DEF that no sibling move fixes, nodes in a parent that accepts
        no such type, unknown nodes).
        `remaining` is the re-validation of `fixed`: schema_valid, schema_errors,
        profile_ok. If remaining.schema_valid is false, the errors are structural;
        fix them by hand, then validate again.

        Args:
            content: The X3D XML content string to repair (inline).
            path: Path to an X3D file to repair instead of inline content.
                  Provide exactly one of `content`, `path` or `blob_id`.
            blob_id: Id of a stored blob holding the document.
            return_mode: "full" (default) returns {fixed, changes, unfixable, remaining}.
                  "summary" returns JSON without `fixed`: its sha256, size and node counts,
                  changes, unfixable and a compact `remaining`. "blob" also stores the fixed
                  document and returns its blob_id.
        """
        try:
            mode = parse_mode(return_mode)
            text, label = await resolve_input(content, path, blob_id, ctx)
            if mode == "blob":
                require_blob_access(ctx)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        result = await run_heavy(_repair_x3d, text, size=len(text))
        if mode == "full":
            return json.dumps(result, indent=2)
        if result.get("error"):
            return error_envelope(mode, result["error"], label)
        remaining = result.get("remaining", {})
        errors = remaining.get("schema_errors", [])
        try:
            doc = await run_heavy(document_info, result["fixed"], XML_TYPE, mode == "blob",
                                  size=len(result["fixed"]))
        except BlobError as exc:
            return error_envelope(mode, f"the result could not be stored: {exc}", label)
        inp = await run_heavy(input_info, text, blob_id, size=len(text))
        return envelope(
            mode, label, ok=True, input=inp, document=doc,
            **cap_items(clip_deep(result["changes"]), "changes"),
            **cap_items(clip_deep(result["unfixable"]), "unfixable"),
            remaining={"schema_valid": remaining.get("schema_valid"),
                       "profile_ok": remaining.get("profile_ok"),
                       "unfixable": remaining.get("unfixable"),
                       "schema_error_count": len(errors),
                       **cap_items([clip(e) for e in errors], "schema_errors")})
