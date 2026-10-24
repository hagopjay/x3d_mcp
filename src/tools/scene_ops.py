"""X3D scene manipulation tools: modify, remove, and move nodes.

Operates on serialized X3D content (file path or inline XML) and returns
the modified XML string. Distinct from the granular tools in
``tools/granular.py`` which mutate the in-memory SceneManager state.

Adapted from scene_manipulation.py in https://github.com/niknarra/x3d-mcp by
Nikhil Narra and Nicholas Polys (Virginia Tech / Web3D Consortium).
"""

import json
import os
from lxml import etree

from mcp.server.fastmcp import Context, FastMCP

from transfer.outputs import (
    XML_TYPE, cap_items, clip_deep, document_info, envelope, error_envelope, input_info,
    parse_mode,
)
from transfer.tooling import require_blob_access, resolve_input, run_heavy


def _http_transport_active() -> bool:
    """Whether the server is running over the Streamable HTTP transport.

    File-path input is a local-use affordance; over HTTP it would let
    remote callers read .x3d files from the server's filesystem.
    """
    transport = os.environ.get("MCP_TRANSPORT", "stdio").lower()
    return transport in ("http", "streamable-http")


def _parse_x3d_source(source: str, inline: bool = False) -> etree._Element:
    """Parse an X3D source (file path or inline XML string) into an lxml tree.

    inline=True says `source` is document text whatever it starts with (a stored
    blob: it may open with a comment or a byte-order mark), so it is never taken
    for a path. Raises ValueError with a descriptive message on failure.
    """
    stripped = source.strip()

    if (inline or stripped.startswith("<?xml") or stripped.startswith("<X3D")
            or stripped.startswith("<!DOCTYPE")):
        try:
            parser = etree.XMLParser(remove_blank_text=True)
            return etree.fromstring(stripped.encode(), parser)
        except etree.XMLSyntaxError as e:
            raise ValueError(f"Invalid X3D XML: {e}")

    if _http_transport_active():
        raise ValueError(
            "File-path input is disabled over the HTTP transport. "
            "Provide inline X3D XML content instead."
        )

    if not os.path.exists(stripped):
        raise ValueError(
            f"File not found: {stripped}. "
            "Provide either an absolute file path to a .x3d file or inline X3D XML."
        )

    if not stripped.lower().endswith(".x3d"):
        raise ValueError(
            f"Unsupported file extension: {os.path.splitext(stripped)[1]}. "
            "Only XML-encoded X3D files (.x3d) are supported."
        )

    try:
        with open(stripped, "r", encoding="utf-8") as f:
            content = f.read()
    except Exception as e:
        raise ValueError(f"Could not read file '{stripped}': {e}")

    try:
        parser = etree.XMLParser(remove_blank_text=True)
        return etree.fromstring(content.encode(), parser)
    except etree.XMLSyntaxError as e:
        raise ValueError(f"Invalid XML in file '{stripped}': {e}")


def _find_scene(tree: etree._Element) -> etree._Element:
    """Find the <Scene> element in a parsed X3D tree."""
    scene = tree.find("Scene")
    if scene is None:
        raise ValueError(
            "No <Scene> element found in the X3D document. "
            "A valid X3D document requires <X3D><Scene>...</Scene></X3D>."
        )
    return scene


def _serialize(tree: etree._Element) -> str:
    return etree.tostring(
        tree, xml_declaration=True, encoding="UTF-8", pretty_print=True
    ).decode()


def _tag(el: etree._Element) -> str:
    return el.tag if isinstance(el.tag, str) else ""


def _find_node_by_def(scene: etree._Element, def_name: str) -> etree._Element | None:
    matches = scene.xpath(f"//*[@DEF='{def_name}']")
    return matches[0] if matches else None


def _find_node_by_type(
    scene: etree._Element, node_type: str, index: int
) -> tuple[etree._Element | None, str]:
    matches = list(scene.iter(node_type))
    if not matches:
        return None, (
            f"No '{node_type}' node found in the scene. "
            f"Use list_nodes to see which node types exist in X3D."
        )
    if index >= len(matches):
        return None, (
            f"Index {index} out of range. The scene contains {len(matches)} "
            f"'{node_type}' node(s) (valid indices: 0-{len(matches) - 1})."
        )
    return matches[index], ""


def _available_defs(scene: etree._Element) -> list[str]:
    return [el.get("DEF") for el in scene.xpath("//*[@DEF]")]


def _is_descendant_of(node: etree._Element, ancestor: etree._Element) -> bool:
    parent = node.getparent()
    while parent is not None:
        if parent is ancestor:
            return True
        parent = parent.getparent()
    return False


def _modify_node(source: str, def_name: str, field_changes: dict,
                 inline: bool = False, log: list | None = None) -> str:
    """Set attributes on a DEF'd node. `log`, when given, receives one record per field set."""
    if not def_name:
        return "A def_name is required to identify the node to modify."
    if not field_changes:
        return "No field_changes provided. Pass a dict of field=value pairs to modify."

    try:
        tree = _parse_x3d_source(source, inline)
    except ValueError as e:
        return str(e)

    try:
        scene = _find_scene(tree)
    except ValueError as e:
        return str(e)

    target = _find_node_by_def(scene, def_name)
    if target is None:
        defs = _available_defs(scene)
        if defs:
            return (
                f"No node with DEF='{def_name}' found. "
                f"Available DEF names: {', '.join(defs)}."
            )
        return (
            f"No node with DEF='{def_name}' found. The scene has no DEF'd nodes."
        )

    for field_name, value in field_changes.items():
        if log is not None:
            log.append({"op": "modify", "def": def_name, "node": _tag(target),
                        "field": field_name, "before": target.get(field_name), "after": str(value)})
        target.set(field_name, str(value))

    return _serialize(tree)


def _remove_node(
    source: str, def_name: str = "", node_type: str = "", index: int = 0,
    inline: bool = False, log: list | None = None,
) -> str:
    """Remove a node and its children. `log`, when given, receives one record on success."""
    if not def_name and not node_type:
        return (
            "Specify either def_name or node_type to identify the node to remove."
        )

    try:
        tree = _parse_x3d_source(source, inline)
    except ValueError as e:
        return str(e)

    try:
        scene = _find_scene(tree)
    except ValueError as e:
        return str(e)

    if def_name:
        target = _find_node_by_def(scene, def_name)
        if target is None:
            defs = _available_defs(scene)
            if defs:
                return (
                    f"No node with DEF='{def_name}' found. "
                    f"Available DEF names: {', '.join(defs)}"
                )
            return (
                f"No node with DEF='{def_name}' found. The scene has no DEF'd nodes."
            )
    else:
        target, error = _find_node_by_type(scene, node_type, index)
        if target is None:
            return error

    if target is scene:
        return "Cannot remove the <Scene> element itself."

    parent = target.getparent()
    if parent is None:
        return "Cannot remove the root element."

    if log is not None:
        log.append({"op": "remove", "node": _tag(target), "def": target.get("DEF"),
                    "parent": _tag(parent), "removed_elements": sum(1 for _ in target.iter())})
    parent.remove(target)
    return _serialize(tree)


def _move_node(source: str, def_name: str, new_parent_def: str = "",
               inline: bool = False, log: list | None = None) -> str:
    """Reparent a DEF'd node. `log`, when given, receives one record on success."""
    if not def_name:
        return "A def_name is required to identify the node to move."

    try:
        tree = _parse_x3d_source(source, inline)
    except ValueError as e:
        return str(e)

    try:
        scene = _find_scene(tree)
    except ValueError as e:
        return str(e)

    target = _find_node_by_def(scene, def_name)
    if target is None:
        defs = _available_defs(scene)
        if defs:
            return (
                f"No node with DEF='{def_name}' found. "
                f"Available DEF names: {', '.join(defs)}"
            )
        return (
            f"No node with DEF='{def_name}' found. The scene has no DEF'd nodes."
        )

    if new_parent_def:
        if new_parent_def == def_name:
            return f"Cannot move a node under itself (DEF='{def_name}')."

        new_parent = _find_node_by_def(scene, new_parent_def)
        if new_parent is None:
            defs = _available_defs(scene)
            return (
                f"New parent DEF='{new_parent_def}' not found. "
                f"Available DEF names: {', '.join(defs)}"
            )

        if _is_descendant_of(new_parent, target):
            return (
                f"Cannot move DEF='{def_name}' under DEF='{new_parent_def}' -- "
                f"'{new_parent_def}' is a descendant of '{def_name}', which would "
                f"create a cycle in the scene graph."
            )
    else:
        new_parent = scene

    old_parent = target.getparent()
    if old_parent is None:
        return "Cannot move the root element."

    if old_parent is new_parent:
        location = "<Scene>" if new_parent is scene else f"DEF={new_parent_def!r}"
        return f"Node DEF='{def_name}' is already a child of {location}."

    if log is not None:
        log.append({"op": "move", "def": def_name, "node": _tag(target),
                    "from_parent": _tag(old_parent), "from_parent_def": old_parent.get("DEF"),
                    "to_parent": _tag(new_parent), "to_parent_def": new_parent.get("DEF")})
    old_parent.remove(target)
    new_parent.append(target)

    return _serialize(tree)


def _looks_like_xml(source: str) -> bool:
    """The test _parse_x3d_source applies to tell inline XML from a file path."""
    return source.strip().startswith(("<?xml", "<X3D", "<!DOCTYPE"))


async def _scene_op(ctx, content: str, blob_id: str, return_mode: str, run) -> str:
    """Run one scene operation with optional blob input and return mode.

    run(source, inline, log) -> the operation's result string: the document, or a message.
    With no blob_id and return_mode "full" this is the original call, untouched.
    """
    try:
        mode = parse_mode(return_mode)
    except ValueError as e:
        return str(e)
    if mode == "full" and not blob_id.strip():
        return await run_heavy(run, content, False, None, size=len(content))

    text, inline, label = content, False, "inline content"
    try:
        if blob_id.strip():
            text, label = await resolve_input(content, "", blob_id, ctx)
            inline = True
        if mode == "blob":
            require_blob_access(ctx)
    except ValueError as e:
        return str(e) if mode == "full" else error_envelope(mode, str(e))

    is_path = not inline and not _looks_like_xml(text)  # content that names a file, as before
    if is_path:
        label = f"file {text.strip()}"
    log: list = []
    result = await run_heavy(run, text, inline, log, size=len(text))
    if mode == "full":
        return result
    if not log:                                        # the operation refused: result is the message
        return error_envelope(mode, result, label)
    try:
        doc = await run_heavy(document_info, result, XML_TYPE, mode == "blob", size=len(result))
        inp = {"path": text.strip()} if is_path else await run_heavy(
            input_info, text, blob_id, size=len(text))
    except ValueError as e:                            # store unavailable, full or over quota
        return error_envelope(mode, str(e), label)
    return envelope(mode, label, ok=True, input=inp, document=doc,
                    **cap_items(clip_deep(log), "changes"))


def register(mcp: FastMCP):

    @mcp.tool()
    async def modify_x3d_node(
        content: str, def_name: str, field_changes: str,
        blob_id: str = "", return_mode: str = "full", ctx: Context = None,
    ) -> str:
        """Modify attribute values on a DEF'd node in an X3D document.

        Updates one or more field values on the named node and returns the
        complete modified X3D document. Operates on serialized X3D content
        (file path or inline XML); does not touch the granular scene state.

        Args:
            content: X3D content (inline XML string or file path to .x3d).
                     Pass an empty string when blob_id is given.
            def_name: DEF name of the node to modify.
            field_changes: JSON object string of {field_name: value} pairs,
                e.g. '{"diffuseColor": "0 1 0"}'.
            blob_id: Id of a stored blob holding the document (instead of content).
            return_mode: "full" (default) returns the document. "summary" returns JSON
                with the result's sha256, size, node counts and the list of changes
                (field, before, after) instead of the document. "blob" does the same and
                also stores the result, returning its blob_id for the next call.
        """
        try:
            changes = json.loads(field_changes) if field_changes else {}
        except json.JSONDecodeError as e:
            return f"Invalid JSON in field_changes: {e}"
        return await _scene_op(
            ctx, content, blob_id, return_mode,
            lambda src, inline, log: _modify_node(src, def_name, changes, inline, log))

    @mcp.tool()
    async def remove_x3d_node(
        content: str,
        def_name: str = "",
        node_type: str = "",
        index: int = 0,
        blob_id: str = "",
        return_mode: str = "full",
        ctx: Context = None,
    ) -> str:
        """Remove a node (and its children) from an X3D document.

        Identify the target either by DEF name OR by node_type plus index.
        Returns the modified X3D document.

        Args:
            content: X3D content (inline XML string or file path to .x3d).
                     Pass an empty string when blob_id is given.
            def_name: DEF name of the node to remove (preferred).
            node_type: Node type name (e.g. 'Transform') if no DEF.
            index: 0-based index among nodes of that type.
            blob_id: Id of a stored blob holding the document (instead of content).
            return_mode: "full" (default) returns the document; "summary" returns JSON
                (sha256, size, node counts, what was removed); "blob" also stores the
                result and returns its blob_id.
        """
        return await _scene_op(
            ctx, content, blob_id, return_mode,
            lambda src, inline, log: _remove_node(src, def_name, node_type, index, inline, log))

    @mcp.tool()
    async def move_x3d_node(
        content: str, def_name: str, new_parent_def: str = "",
        blob_id: str = "", return_mode: str = "full", ctx: Context = None,
    ) -> str:
        """Reparent a DEF'd node in an X3D document.

        If new_parent_def is empty, moves the node to be a direct child of
        <Scene>. Rejects moves that would create a cycle (moving a node
        under one of its own descendants) or move a node under itself.

        Args:
            content: X3D content (inline XML string or file path to .x3d).
                     Pass an empty string when blob_id is given.
            def_name: DEF name of the node to move.
            new_parent_def: DEF name of the new parent (empty = Scene root).
            blob_id: Id of a stored blob holding the document (instead of content).
            return_mode: "full" (default) returns the document; "summary" returns JSON
                (sha256, size, node counts, old and new parent); "blob" also stores the
                result and returns its blob_id.
        """
        return await _scene_op(
            ctx, content, blob_id, return_mode,
            lambda src, inline, log: _move_node(src, def_name, new_parent_def, inline, log))
