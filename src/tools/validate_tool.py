"""X3D validation MCP tools.

Exposes validation pipeline as MCP tools.
"""

import json
from mcp.server.fastmcp import FastMCP

from validation.validate import validate_xml, validate_json
from validation.semantic import validate_semantic as _validate_semantic
from mcp.server.fastmcp import Context
from validation.autofix import autofix_containerfields
from validation.repair import repair_x3d as _repair_x3d
from tools.granular import _scene_for
from x3d_utils.source import load_x3d_source


def register(mcp: FastMCP):

    @mcp.tool()
    def validate_x3d(content: str = "", encoding: str = "xml", path: str = "") -> str:
        """Validate X3D against the XSD schema for its declared version (inline content OR a file path).

        XML is checked against the schema matching the document's `version`
        attribute: 4.0 (ratified) or 4.1 (draft, the default when no version is
        given). A node that exists only in a later version (EnvironmentLight,
        GaussianSplats, HAnimPose, ...) is named in `notes` with the version
        that has it. JSON is checked against the X3D 4.0 JSON Schema.

        Args:
            content: The X3D content string to validate (inline).
            encoding: Content encoding - "xml" or "json".
            path: Path to an X3D file to validate instead of inline content.
                  Provide exactly one of `content` or `path`.
        """
        try:
            text = load_x3d_source(content, path)
        except ValueError as exc:
            return json.dumps({"valid": False, "errors": [str(exc)]}, indent=2)
        if encoding == "xml":
            result = validate_xml(text)
        elif encoding == "json":
            result = validate_json(text)
        else:
            result = {"valid": False, "errors": [f"Unsupported encoding: {encoding}"]}
        return json.dumps(result, indent=2)

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
    def validate_semantic(content: str = "", path: str = "") -> str:
        """Run semantic checks on X3D beyond XSD schema validation (content OR path).

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
                  Provide exactly one of `content` or `path`.
        """
        try:
            text = load_x3d_source(content, path)
        except ValueError as exc:
            return f"# Semantic Check: Input Error\n\n{exc}"
        return _validate_semantic(text)

    @mcp.tool()
    def autofix_x3d(content: str = "", path: str = "") -> str:
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
                  Provide exactly one of `content` or `path`.
        """
        try:
            text = load_x3d_source(content, path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        return json.dumps(autofix_containerfields(text), indent=2)

    @mcp.tool()
    def repair_x3d(content: str = "", path: str = "") -> str:
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
                  Provide exactly one of `content` or `path`.
        """
        try:
            text = load_x3d_source(content, path)
        except ValueError as exc:
            return json.dumps({"error": str(exc)}, indent=2)
        return json.dumps(_repair_x3d(text), indent=2)
