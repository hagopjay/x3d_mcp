"""Node-restricted X3D JSON Schema for constrained LLM decoding.

The Web3D X3D 4.0 JSON Schema (with the NavigationInfo fix, validation.validate.load_json_schema) is
about 500 KB minified, far too much to put in a prompt or a structured-output request. A model rarely
needs more than a few dozen nodes, so `restricted_schema` keeps the schema's structure and field typing
but allows only a chosen vocabulary, then prunes every definition that is no longer reachable.

Same transform as x3d.loom/x3d.zod/src/restrict.ts; the two are checked against each other there.
"""

import copy
import hashlib
import json

from validation.validate import JSON_SCHEMA_PATH, load_json_schema

# Keys dropped in lean mode: class/id/style attributes, comments, IS, metadata (and $comment/default/description).
LEAN_KEYS = {"@class", "@id", "@style", "IS", "-metadata", "#comment"}

PRESETS = {
    "starter": ["Transform", "Group", "Shape", "Appearance", "Material", "Box", "Sphere", "Cone",
                "Cylinder", "Viewpoint", "NavigationInfo", "WorldInfo"],
    "animated": ["Transform", "Group", "Shape", "Appearance", "Material", "Box", "Sphere", "Cone",
                 "Cylinder", "Viewpoint", "NavigationInfo", "WorldInfo", "TimeSensor",
                 "PositionInterpolator", "OrientationInterpolator", "ScalarInterpolator",
                 "DirectionalLight", "PointLight", "Background"],
}


def node_def_names(schema: dict) -> list[str]:
    """Scene nodes: definitions that carry a USE form. Everything else (head, meta, ROUTE...) is structural."""
    return [k for k, d in schema["$defs"].items()
            if not k.startswith(("-", "@")) and '"@USE"' in json.dumps(d)]


def restricted_schema(nodes, lean: bool = True, schema: dict | None = None) -> dict:
    """The schema limited to `nodes`. Unknown names raise ValueError so a typo cannot silently shrink the vocabulary."""
    out = copy.deepcopy(schema) if schema is not None else load_json_schema()
    known = set(node_def_names(out))
    allowed = set(nodes)
    unknown = sorted(allowed - known)
    if unknown:
        raise ValueError(f"not X3D 4.0 scene nodes: {', '.join(unknown)}")

    def walk(n):
        if isinstance(n, list):
            for v in n:
                walk(v)
        elif isinstance(n, dict):
            props = n.get("properties")
            if props:
                for k in list(props):
                    if (k in known and k not in allowed) or (lean and k in LEAN_KEYS):
                        del props[k]
                if "required" in n:
                    n["required"] = [k for k in n["required"] if k in props]
            if lean:
                for k in ("$comment", "default", "description"):
                    n.pop(k, None)
            for v in list(n.values()):
                walk(v)

    walk(out["$defs"])
    walk(out["properties"])
    seen: set[str] = set()

    def visit(n):
        if isinstance(n, list):
            for v in n:
                visit(v)
        elif isinstance(n, dict):
            ref = n.get("$ref")
            if isinstance(ref, str):
                name = ref.removeprefix("#/$defs/")
                if name not in seen:
                    seen.add(name)
                    visit(out["$defs"][name])
            for v in list(n.values()):
                visit(v)

    visit(out["properties"])
    out["$defs"] = {k: v for k, v in out["$defs"].items() if k in seen}
    return out


def schema_report(schema: dict) -> dict:
    """Sizes and provenance for a restricted schema, so every number names its source."""
    text = json.dumps(schema, separators=(",", ":"))
    return {
        "bytes": len(text.encode()),
        "defs": len(schema["$defs"]),
        "upstream_sha1": hashlib.sha1(JSON_SCHEMA_PATH.read_bytes()).hexdigest(),
        "full_bytes": len(json.dumps(load_json_schema(), separators=(",", ":")).encode()),
    }
