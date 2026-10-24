"""A small, strict JSON Schema for generating X3D JSON, cut from the official 4.0 schema.

Providers that constrain decoding to a JSON Schema accept only a modest schema,
and the official one is 1 MB. This builds a subset limited to a profile's nodes
(plus any the caller names), with the keywords a decoder uses and nothing else:
every object is closed (additionalProperties false), enumerations stay closed,
head.component can only name real components, and
NavigationInfo.@type is the array the field really is.

It is not a new language. An instance of the subset is valid against the official
schema (with the NavigationInfo correction from validation.validate), which is why
the root requires "encoding" and, for version 4.1, which the 4.0 enum lacks, the
two differ on @version only. It is a first filter for generation, never the gate:
the exact validators (XSD, semantic checks) still decide.

Left out, to keep the size down: IS and PROTO statements, -metadata, @class/@id/@style,
bounding-box fields, @geoSystem (its oneOf has nine alternatives) and the secondary
texture mappings of the material nodes.
"""

import copy
import json

from validation.profile import profile_nodes
from validation.validate import load_json_schema
from x3d_utils.x3duom import get_x3duom

SUPPORTED_VERSIONS = ("4.0", "4.1")
DEFAULT_MAX_NODES = 70

# Nodes offered first when a profile holds more than max_nodes: how often they
# occur in the 65-scene audit set, then the everyday authoring vocabulary.
PRIORITY = (
    "Shape", "Appearance", "Material", "Transform", "Viewpoint", "WorldInfo", "Box", "Sphere",
    "Cylinder", "Cone", "Group", "DirectionalLight", "PointLight", "SpotLight", "Background",
    "NavigationInfo", "Coordinate", "IndexedFaceSet", "IndexedLineSet", "PointSet", "TimeSensor",
    "OrientationInterpolator", "PositionInterpolator", "ScalarInterpolator", "ColorInterpolator",
    "CoordinateInterpolator", "TouchSensor", "ImageTexture", "TextureCoordinate", "TextureTransform",
    "Color", "ColorRGBA", "Normal", "Text", "FontStyle", "IndexedTriangleSet", "TriangleSet", "QuadSet",
    "ElevationGrid", "Extrusion", "Fog", "Switch", "LOD", "Billboard", "Anchor", "Collision",
    "StaticGroup", "Inline", "OrthoViewpoint", "ViewpointGroup", "BooleanToggle", "BooleanFilter",
    "TimeTrigger", "PlaneSensor", "SphereSensor", "CylinderSensor", "ProximitySensor",
    "VisibilitySensor", "Script", "LineProperties", "PointProperties", "FillProperties",
    "UnlitMaterial", "PhysicalMaterial", "TwoSidedMaterial", "MultiTexture", "PixelTexture",
    "Rectangle2D", "Circle2D", "Arc2D", "Disk2D", "Polyline2D", "IndexedQuadSet", "LineSet",
    "GeoLocation", "GeoViewpoint", "GeoCoordinate", "GeoPositionInterpolator", "GeoElevationGrid",
    "HAnimHumanoid", "HAnimJoint", "HAnimSegment", "HAnimSite",
)

# Slots that make a node useful, so choosing the node also chooses one filler.
COMPANIONS = {
    "Shape": ("Appearance",), "Appearance": ("Material",),
    **{n: ("Coordinate",) for n in (
        "IndexedFaceSet", "IndexedLineSet", "PointSet", "LineSet", "IndexedTriangleSet",
        "IndexedQuadSet", "TriangleSet", "QuadSet", "TriangleStripSet", "TriangleFanSet",
        "IndexedTriangleStripSet", "IndexedTriangleFanSet")},
    "HAnimHumanoid": ("HAnimJoint", "HAnimSegment"), "HAnimJoint": ("HAnimSegment",),
}

# Members of the official node definitions that are not generated (see the module docstring).
_DROPPED_FIELDS = frozenset({"IS", "-metadata", "@class", "@id", "@style"})
_MATERIALS = frozenset({"Material", "PhysicalMaterial", "UnlitMaterial", "TwoSidedMaterial"})
_KEEP = frozenset({"type", "enum", "items", "minItems", "maxItems", "properties", "required",
                   "additionalProperties", "minimum", "maximum", "exclusiveMinimum", "exclusiveMaximum"})
_EXCLUDED_NODES = frozenset({"ProtoInstance"})
_OMITTED = ["IS", "ProtoDeclare/ProtoInstance", "-metadata", "@class", "@id", "@style (except FontStyle's)",
            "@bbox*", "@geoSystem (a 9-way oneOf; the default GD WE applies)",
            "secondary texture mappings of the material nodes", "head.unit", "meta attributes beyond name/content"]


class SchemaRequestError(ValueError):
    """The profile, version, node names or max_nodes cannot be turned into a schema."""


def _clean(value):
    """Keep the keywords a constrained decoder uses; an open enum (anyOf enum|string) becomes its base type."""
    if not isinstance(value, dict):
        return value
    if "anyOf" in value and all(isinstance(a, dict) for a in value["anyOf"]):
        return {"type": value.get("type", "string")}
    out = {}
    for key, sub in value.items():
        if key == "properties":
            out[key] = {name: _clean(p) for name, p in sub.items()}
        elif key in _KEEP:
            out[key] = _clean(sub)
    if "prefixItems" in value or ("items" in out and not isinstance(out["items"], dict)):
        out["items"] = _item_bounds(value)
    return out


def _item_bounds(array_spec: dict) -> dict:
    """One items schema for an array the official schema describes with prefixItems (items:false for SF tuples).

    The tightest bound any position carries is applied to all of them, so the result
    stays inside the official schema.
    """
    positions = [p for p in array_spec.get("prefixItems", []) if isinstance(p, dict)]
    rest = array_spec.get("items") if isinstance(array_spec.get("items"), dict) else {}
    item = {"type": rest.get("type") or next((p["type"] for p in positions if "type" in p), "number")}
    for key, pick in (("minimum", max), ("exclusiveMinimum", max), ("maximum", min), ("exclusiveMaximum", min)):
        bounds = [p[key] for p in [*positions, rest] if key in p]
        if bounds:
            item[key] = pick(bounds)
    return item


def _order(name: str) -> tuple:
    """Strict grammars emit properties in schema order, so make it the order a scene reads best in."""
    group = 0 if name == "@DEF" else 1 if name.startswith("@") else 3 if name.startswith("-") else 2
    return (group, name.lower())


def _field_value() -> dict:
    scalar = [{"type": "string"}, {"type": "number"}, {"type": "boolean"}]
    return {"anyOf": scalar + [{"type": "array", "items": {"anyOf": scalar}}]}


class _Builder:
    def __init__(self, official: dict, chosen: list[str], component_names: list[str],
                 profile: str, version: str):
        self.defs = official["$defs"]
        self.chosen = set(chosen) | {"ROUTE"}
        self.out: dict[str, dict] = {}
        self.unions: dict[str, str] = {}
        self.profile, self.version, self.component_names = profile, version, component_names

    def union(self, key: str) -> str | None:
        """Def name of an official node-list union restricted to the chosen nodes (None when none is chosen)."""
        if key in self.unions:
            return self.unions[key] or None
        raw = copy.deepcopy(self.defs[key])
        is_array = raw.get("type") == "array"
        members = [n for n in (raw["items"] if is_array else raw).get("properties", {}) if n in self.chosen]
        if not members:
            self.unions[key] = ""
            return None
        name = key.lstrip("-") if len(key) <= 40 else f"U{len(self.unions) + 1}"
        self.unions[key] = name
        holder = {"type": "object", "properties": {n: {"$ref": f"#/$defs/{n}"} for n in members},
                  "additionalProperties": False, "minProperties": 1, "maxProperties": 1}
        self.out[name] = {"type": "array", "minItems": 1, "items": holder} if is_array else holder
        for n in members:
            self.node(n)
        return name

    def statement_field(self) -> None:
        """Script/shader field declarations: name, type and access type, with a typed value."""
        if "field" in self.out:
            return
        item = copy.deepcopy(self.defs["field"]["items"])
        props = {n: _clean(p) for n, p in item["properties"].items()
                 if n not in _DROPPED_FIELDS and n not in ("-children", "@appinfo", "@documentation")}
        props["@value"] = _field_value()
        item = {"type": "object", "properties": dict(sorted(props.items(), key=lambda kv: _order(kv[0]))),
                "required": ["@name", "@type", "@accessType"], "additionalProperties": False}
        self.out["field"] = {"type": "array", "items": item}

    def node(self, name: str) -> None:
        if name in self.out:
            return
        self.out[name] = {}                                   # placeholder: breaks reference cycles
        raw = copy.deepcopy(self.defs[name])
        reusable = "oneOf" in raw                             # a DEF-able node may be replaced by {"@USE": ...}
        if reusable:
            raw = raw["oneOf"][1]
        props = {}
        for field, spec in raw.get("properties", {}).items():
            if field.startswith("@bbox") or (field in _DROPPED_FIELDS and "enum" not in spec):
                continue                                      # an enum marks FontStyle's real @style field
            if name in _MATERIALS and (field.endswith("Mapping") or (
                    field.startswith("-") and field.endswith("Texture")
                    and field not in ("-baseTexture", "-diffuseTexture"))):
                continue
            target = spec["$ref"].split("/")[-1] if "$ref" in spec else None
            if target is None:
                props[field] = _clean(spec)
            elif target.startswith("-"):
                if target != "-childStatements" and (union := self.union(target)):
                    props[field] = {"$ref": f"#/$defs/{union}"}
            elif target == "field":
                self.statement_field()
                props[field] = {"$ref": "#/$defs/field"}
            elif target in self.chosen:
                self.node(target)
                props[field] = {"$ref": f"#/$defs/{target}"}
        full = {"type": "object", "properties": dict(sorted(props.items(), key=lambda kv: _order(kv[0]))),
                "additionalProperties": False}
        if raw.get("required"):
            full["required"] = [r for r in raw["required"] if r in props]
        if reusable:
            use = {"type": "object", "properties": {"@USE": {"type": "string"}},
                   "required": ["@USE"], "additionalProperties": False}
            self.out[name] = {"anyOf": [use, full]}
        else:
            self.out[name] = full

    def root(self) -> dict:
        scene_union = self.union("-X3DChildNode-X3DMetadataObject-LayerSetMFNode")
        scene = {"type": "object", "properties": {"-children": {"$ref": f"#/$defs/{scene_union}"}},
                 "additionalProperties": False}
        head = {"type": "object", "additionalProperties": False, "properties": {}}
        head["properties"]["component"] = {"type": "array", "items": {   # real component names only: the
            "type": "object", "required": ["@level", "@name"], "additionalProperties": False,   # invented ones
            "properties": {"@level": {"type": "integer", "minimum": 1, "maximum": 5},    # ("Appearance") fail
                           "@name": {"enum": self.component_names}}}}
        head["properties"]["meta"] = {"type": "array", "items": {
            "type": "object", "required": ["@content", "@name"], "additionalProperties": False,
            "properties": {"@content": {"type": "string"}, "@name": {"type": "string"}}}}
        defs = dict(self.out)
        defs["Scene"] = scene
        defs["head"] = head
        x3d = {"type": "object", "additionalProperties": False,
               "required": ["encoding", "@profile", "@version", "Scene"],
               "properties": {"encoding": {"enum": ["UTF-8"]}, "@profile": {"enum": [self.profile]},
                              "@version": {"enum": [self.version]}, "head": {"$ref": "#/$defs/head"},
                              "Scene": {"$ref": "#/$defs/Scene"}}}
        return {"type": "object", "required": ["X3D"], "additionalProperties": False,
                "properties": {"X3D": x3d}, "$defs": dict(sorted(defs.items()))}


def _keywords(schema) -> list[str]:
    found: set[str] = set()

    def walk(node):
        if not isinstance(node, dict):
            return
        for key, sub in node.items():
            if key in ("properties", "$defs"):
                for child in sub.values():
                    walk(child)
                continue
            found.add(key)
            if key in ("items", "additionalProperties"):
                walk(sub)
            elif key == "anyOf":
                for child in sub:
                    walk(child)
    walk(schema)
    return sorted(found)


def _count_properties(schema: dict) -> int:
    return sum(len(d.get("properties", {})) + sum(len(a.get("properties", {})) for a in d.get("anyOf", []))
               for d in schema["$defs"].values())


def build_generation_schema(profile: str = "Interchange", nodes: list[str] | None = None,
                            max_nodes: int = DEFAULT_MAX_NODES, version: str = "4.0") -> dict:
    """Return {"schema": <JSON Schema>, "report": {...}} for a profile.

    nodes are added to the profile's vocabulary (and listed under outside_profile
    with the component statement each needs); max_nodes caps the profile's nodes
    taken in PRIORITY order, but never the nodes asked for.
    """
    uom = get_x3duom().get_concrete_nodes()
    official = load_json_schema()
    defs = official["$defs"]
    available = {n for n in uom if n in defs and n not in _EXCLUDED_NODES}
    by_profile = profile_nodes()
    if profile not in by_profile:
        raise SchemaRequestError(f"Unknown profile '{profile}'; use one of {', '.join(sorted(by_profile))}.")
    if version not in SUPPORTED_VERSIONS:
        raise SchemaRequestError(f"Unsupported version '{version}'; use one of {', '.join(SUPPORTED_VERSIONS)}.")
    if max_nodes < 1:
        raise SchemaRequestError("max_nodes must be at least 1.")
    allowed = available if by_profile[profile] is None else by_profile[profile] & available

    asked = list(dict.fromkeys(nodes or []))
    unknown = [n for n in asked if n not in available]
    wanted = [n for n in asked if n in available]
    ranked = sorted(allowed, key=lambda n: (PRIORITY.index(n) if n in PRIORITY else len(PRIORITY), n))

    chosen: list[str] = []
    for name in wanted:                                       # asked-for nodes always come in, with their companions
        chosen.extend(n for n in [name, *COMPANIONS.get(name, ())] if n in available and n not in chosen)
    budget = max(max_nodes, len(chosen))
    for name in ranked:
        if name in chosen:
            continue
        group = [name, *(c for c in COMPANIONS.get(name, ()) if c in available and c not in chosen)]
        if len(chosen) + len(group) <= budget:
            chosen.extend(group)
    dropped = [n for n in ranked if n not in chosen]

    outside = {n: {"component": uom[n]["component"], "level": uom[n]["level"] or 1}
               for n in chosen if by_profile[profile] is not None and n not in by_profile[profile]}
    components: dict[str, int] = {}                           # declare the highest level any chosen node needs
    for info in outside.values():
        if info["component"]:
            components[info["component"]] = max(info["level"], components.get(info["component"], 0))

    names = sorted({uom[n]["component"] for n in available if uom[n]["component"]} | {"Core"})
    schema = _Builder(official, sorted(chosen), names, profile, version).root()
    text = json.dumps(schema, separators=(",", ":"))
    report = {
        "profile": profile, "version": version, "node_count": len(chosen), "nodes": sorted(chosen),
        "unknown_nodes": unknown, "outside_profile": outside,
        "components_to_declare": components,
        "dropped_by_max_nodes": dropped,
        "size": {"bytes": len(text.encode()), "defs": len(schema["$defs"]),
                 "object_properties": _count_properties(schema)},
        "keywords": _keywords(schema), "omitted": list(_OMITTED),
    }
    return {"schema": schema, "report": report}
