"""MCP prompts for guided multi-step X3D workflows.

Prompts steer the LLM through a known-good tool sequence for tasks where
ordering matters (build, audit, animate, convert). Each prompt returns
plain-text guidance referencing the tool names this server actually
exposes.

Adapted from server.py prompt registrations in
https://github.com/niknarra/x3d-mcp by Nikhil Narra and Nicholas Polys
(Virginia Tech / Web3D Consortium). Prompt bodies have been rewritten
to use this server's tool names; Nikhil's references several tools
that don't exist here (e.g. x3d_parse_scene, x3d_scene_template).
"""

from mcp.server.fastmcp import FastMCP


def register(mcp: FastMCP):

    @mcp.prompt()
    def build_scene(description: str = "a simple 3D scene") -> str:
        """Step-by-step guide to build an X3D scene from scratch.

        Walks through node lookup, scene composition (workflow or granular),
        validation, and browser preview via X3DOM.
        """
        return (
            f"Build an X3D scene: {description}\n\n"
            "Two paths are available -- pick whichever fits the task.\n\n"
            "## Path A: Workflow mode (fewer tool calls, high-level)\n\n"
            "1. **Browse the spec** if you need to: `list_components()` for the "
            "37-component overview, `list_nodes(component='Geometry3D')` to see "
            "geometry options, `describe_node('<NodeName>')` for full field info.\n\n"
            "2. **Generate the whole scene in one call** using one of:\n"
            "   - `create_scene(description, profile, encoding)` for a complete scene\n"
            "   - `create_geometry(...)` for a single shape with material/transform\n"
            "   - `compose_scene(...)` to combine multiple objects, lights, viewpoint\n\n"
            "3. **Validate** with `validate_x3d(content)` (XSD), then "
            "`validate_semantic(content)` for authoring issues (missing geometry, "
            "broken ROUTEs, duplicate DEFs).\n\n"
            "4. **Render and LOOK** with `render_image(content)` -- it returns a PNG so "
            "you can see the scene and confirm it's framed, lit, and shaped right "
            "(use `x3dom_page(content)` instead if you want an interactive browser page). "
            "Do not call the scene done until you've rendered it.\n\n"
            "## Path B: Granular mode (full control, node-by-node)\n\n"
            "1. **Reset the in-memory scene:** `reset_scene()`.\n\n"
            "2. **Build the graph:** `create_node('<Type>')` returns a node id, then "
            "`set_field(id, 'fieldName', value)`, `add_child(parent_id, child_id)`, "
            "`def_node(id, 'Name')` / `use_node('Name')` for DEF/USE, and "
            "`add_route(...)` for events.\n\n"
            "3. **Inspect** with `get_scene(encoding='xml')` whenever you want to "
            "see the current state.\n\n"
            "4. **Validate** with `validate_current_scene()` (XSD) plus "
            "`validate_semantic(get_scene())` for authoring checks.\n\n"
            "5. **Render** by passing `get_scene()` to `x3dom_page(...)`.\n\n"
            "## Key X3D patterns\n"
            "- **Shape** = Appearance (Material + optional Texture) + Geometry "
            "(Box, Sphere, Cylinder, IndexedFaceSet, ...)\n"
            "- **Transform** wraps children with translation / rotation / scale\n"
            "- **DEF/USE** lets you define a node once and reuse it\n"
            "- **SFColor** is 3 floats in [0,1] (e.g. '1 0 0' = red)\n"
            "- **SFRotation** is axis-angle: 'x y z angle_in_radians'"
        )

    @mcp.prompt()
    def audit_scene(content: str = "") -> str:
        """Guide to audit an existing X3D document for schema and authoring issues.

        Use this when you have an X3D scene (file path or inline XML) and want
        to surface validation errors, semantic problems, and field-value drift.
        """
        target = "the X3D content" if not content else f"the X3D content at {content!r}"
        return (
            f"Audit {target}.\n\n"
            "Follow these steps using the X3D MCP tools:\n\n"
            "1. **Schema validation:** Call `validate_x3d(content)` to check the "
            "document against the official X3D 4.1 XSD. This catches unknown nodes, "
            "wrong attribute types, missing required fields, and structural errors.\n\n"
            "2. **Semantic validation:** Call `validate_semantic(content)` to surface "
            "authoring-level bugs the schema cannot catch: Shapes without geometry, "
            "empty grouping nodes, duplicate DEFs, USE references to non-existent "
            "DEFs, ROUTE field/access-type/type-match problems, missing Viewpoint.\n\n"
            "3. **Inspect suspect nodes:** For any node type that looks problematic, "
            "call `describe_node('<NodeType>')` to see the canonical field "
            "definitions, default values, ranges, and access types. Compare against "
            "what the document has.\n\n"
            "4. **Apply fixes if needed:**\n"
            "   - `autofix_x3d(content)` to auto-correct containerField mistakes and "
            "get the fixed document back (review any `ambiguous` choices it reports)\n"
            "   - `modify_x3d_node(content, def_name, field_changes)` to update a "
            "field on a DEF'd node\n"
            "   - `remove_x3d_node(content, def_name=...)` to drop a problem node\n"
            "   - `move_x3d_node(content, def_name, new_parent_def)` to reparent\n\n"
            "5. **Re-validate** after each fix and **render** with "
            "`render_image(content)` (or `path=...`) to inspect the result visually.\n\n"
            "Note: this server doesn't yet expose a tree-view / DEF-listing / "
            "stats tool for serialized scenes -- the validators above are how we "
            "see what's wrong. If you need raw structure, the granular mode "
            "(`reset_scene` then re-parse) is the workaround."
        )

    @mcp.prompt()
    def semantic_audit(content: str = "", focus: str = "") -> str:
        """Audit what an X3D scene means: its RDF graph, SPARQL questions, SHACL shapes, the validators, receipts.

        Use this to check a scene you did not write, or to verify your own before calling it done.
        """
        target = "the X3D scene" if not content else f"the X3D scene at {content!r}"
        extra = f"\nPay particular attention to: {focus}\n" if focus else ""
        return (
            f"Audit the meaning of {target}, layer by layer, and keep receipts.{extra}\n"
            "1. **Graph it:** `scene_to_rdf(content|path|blob_id, return_mode='summary')`. Keep the "
            "`graph_id`, the triple count, the `warnings` and the `instrument`. Every later call takes "
            "`graph_id=` instead of the document.\n\n"
            "2. **Ask it:** `sparql_scene(graph_id=..., sparql=...)`. Start with an inventory, then follow "
            "what you find. Useful queries (x3do: is pre-declared):\n"
            "   - census: `SELECT ?t (COUNT(?n) AS ?k) WHERE { ?n a ?t . FILTER(STRSTARTS(STR(?t), STR(x3do:))) } "
            "GROUP BY ?t`\n"
            "   - all geometry, through the class hierarchy: `SELECT ?n ?t WHERE { ?n a ?t . "
            "?t rdfs:subClassOf* x3do:X3DGeometryNode }`\n"
            "   - Shapes without an Appearance: `SELECT ?s WHERE { ?s a x3do:Shape . "
            "FILTER NOT EXISTS { ?s x3do:hasAppearance ?a } FILTER NOT EXISTS { ?s owl:sameAs ?d } }`\n"
            "   - DEF/USE reuse: `SELECT ?use ?def WHERE { ?use owl:sameAs ?def }`\n"
            "   - what a ROUTE drives: `SELECT ?from ?ff ?to ?tf WHERE { ?r a x3do:ROUTE ; x3do:fromNode ?from ; "
            "x3do:fromField ?ff ; x3do:toNode ?to ; x3do:toField ?tf }`\n\n"
            "3. **Hold it to the shapes:** `shacl_validate(graph_id=...)` with the bundled x3d-core shapes "
            "(colour ranges, Shape geometry, USE type, ROUTE endpoints, field domains, child nodes, Viewpoint). "
            "For each result note focus, def, check_id and fix_hint. Add your own rule with "
            "`extra_shapes_ttl` when the task needs one. Keep inference='none': reasoning over this ontology "
            "adds types that are not in the scene.\n\n"
            "4. **Gate it:** `validate_x3d(content)` (XSD) and `validate_semantic(content)` (containerField, "
            "USE-before-DEF, ROUTE types and order, profile). RDF keeps no document order, so these catch what "
            "the graph cannot. A scene passes only when both are clean.\n\n"
            "5. **Explain placement questions** with `ontology_vs_gate('<NodeType>')`: the ontology's map "
            "proposes parents, `where_can_place` and the XSD decide. `rdf_infer(graph_id=...)` (default "
            "profile 'subclass') shows class membership; its 'rdfs' and 'owl-rl' profiles are for studying "
            "the ontology, not for validating a scene.\n\n"
            "6. **Receipts:** for each finding you verified, `claim_append(claim, grade, subject, instrument, "
            "evidence)`: grade Confirmed only when a tool verdict shows it, Likely when it follows from one, "
            "Suspected otherwise; instrument = the JSON `instrument` the tool returned; evidence = graph_sha256 "
            "plus the check_id or query. Finish with `claims_verify`.\n\n"
            "7. **Fix and repeat:** `autofix_x3d` / `repair_x3d` / `modify_x3d_node`, then steps 1, 3 and 4 "
            "again until shacl_validate has no Violation and both validators are clean. Render with "
            "`render_image` before calling it done: valid is not the same as visible.\n\n"
            "Report which layer caught each problem (SPARQL, SHACL, XSD, validate_semantic, render). "
            "Names like Shape_2_4_2 are positions and change when nodes are inserted; DEF names are stable."
        )

    @mcp.prompt()
    def animate_scene(target_description: str = "a rotating object") -> str:
        """Step-by-step guide to add animation to an X3D scene."""
        return (
            f"Add animation to an X3D scene: {target_description}\n\n"
            "Follow these steps:\n\n"
            "1. **Get the animation overview** with `x3d_animation_info()` -- one "
            "page on TimeSensor + Interpolator + ROUTE, the three pieces every "
            "X3D animation needs.\n\n"
            "2. **Identify the target field's type:** Call "
            "`describe_node('<NodeType>')` on the node you want to animate. The "
            "interpolator is chosen from the field's type:\n"
            "   - `SFRotation` -> OrientationInterpolator (rotation, orientation)\n"
            "   - `SFVec3f` -> PositionInterpolator (translation, scale, position)\n"
            "   - `SFColor` -> ColorInterpolator (diffuseColor, emissiveColor)\n"
            "   - `SFFloat` -> ScalarInterpolator (transparency, intensity)\n\n"
            "3. **Look up patterns** with `x3d_animation_info('examples')` for "
            "ready-to-paste XML for rotation, color pulse, path animation, fade.\n\n"
            "4. **Generate the chain in one call:** "
            "`animate_x3d_node(content, target_def='<DEF>', field_name='<field>', "
            "from_value='<start>', to_value='<end>', duration=<seconds>, loop=True)`. "
            "This auto-selects the interpolator, generates DEF names "
            "(`<DEF>_<field>_Timer`, `<DEF>_<field>_Interp`), and inserts a "
            "TimeSensor + Interpolator + 2 ROUTEs at the Scene level.\n\n"
            "5. **Validate the result:** `validate_x3d(animated)` for XSD, then "
            "`validate_semantic(animated)` to confirm the new ROUTEs are sane.\n\n"
            "6. **Render** with `x3dom_page(animated, title='Animated: ...')` and "
            "open the resulting HTML in a browser to watch the animation.\n\n"
            "## When the auto-generator isn't enough\n"
            "Use `add_x3d_route(content, from_node, from_field, to_node, to_field)` "
            "to insert a single ROUTE with full validation. Useful for fan-out "
            "(one source -> several targets) and for non-Interpolator wiring "
            "(e.g. TouchSensor.touchTime -> TimeSensor.startTime)."
        )

    @mcp.prompt()
    def convert_to_x3dom() -> str:
        """Guide to convert X3D content into a browser-viewable X3DOM HTML page."""
        return (
            "Convert X3D content to an X3DOM HTML page for browser viewing.\n\n"
            "Follow these steps:\n\n"
            "1. **Validate first** with `validate_x3d(content)` -- X3DOM is more "
            "forgiving than the XSD, but invalid X3D produces unpredictable browser "
            "output. Fix any errors before converting.\n\n"
            "2. **Convert** by calling `x3dom_page(content, title='...', "
            "width='1280px', height='720px')` (the HD 16:9 default). The tool "
            "accepts either a full X3D "
            "document or a raw scene fragment, lowercases tag and attribute names "
            "(X3DOM runs in the HTML5 parser which requires lowercase), and "
            "embeds X3DOM 1.8.2 from CDN -- no install needed.\n\n"
            "3. **Save and open:** Write the returned HTML string to a `.html` "
            "file and open it in any modern browser (Chrome, Firefox, Safari, "
            "Edge).\n\n"
            "## Options\n"
            "- `width` / `height` accept any CSS unit: `'100%'`, `'100vh'`, "
            "`'720px'`, etc.\n"
            "- `show_stats=True` shows the X3DOM frame-stats overlay (FPS, draw "
            "calls, triangle count). Useful for performance audits.\n"
            "- `show_log=True` shows the X3DOM log panel (parse warnings, "
            "rendering messages).\n\n"
            "## For a known-good baseline\n"
            "If the converted page renders as a blank canvas and you want to "
            "rule out the page chrome itself, call `x3dom_starter()` to get a "
            "minimal working example page. If that renders correctly in your "
            "browser, the issue is in the X3D content rather than the page."
        )
