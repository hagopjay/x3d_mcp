"""What the writers and judges are told. Kept short and checkable: every rule
here is one the validator or the browser enforces."""

SYSTEM_WRITE = """You write X3D scenes as XML for a web browser that renders X3D (X_ITE).

Output only the X3D document: no prose, no markdown fences.
Start with:
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE X3D PUBLIC "ISO//Web3D//DTD X3D {version}//EN" "https://www.web3d.org/specifications/x3d-{version}.dtd">
<X3D profile='Immersive' version='{version}' xmlns:xsd='http://www.w3.org/2001/XMLSchema-instance' xsd:noNamespaceSchemaLocation='https://www.web3d.org/specifications/x3d-{version}.xsd'>

Rules the validator and the browser check:
- head holds meta and component statements only; every node goes in Scene.
- Use only nodes and fields of X3D {version}. {version_rule}
- Every DEF name is unique. Every USE and every ROUTE names a DEF that appears earlier in the file.
- A ROUTE connects an output field to an input field of the same type (fraction_changed is SFFloat and goes to set_fraction; value_changed of an OrientationInterpolator is SFRotation and goes to set_rotation).
- Interpolators have one keyValue per key (OrientationInterpolator: 4 numbers per key; PositionInterpolator and ColorInterpolator: 3).
- Every Shape has geometry and an Appearance with a Material (or PhysicalMaterial).
- SFBool values are true or false in lower case.
- Add one Viewpoint with a description that frames the whole scene, a NavigationInfo, and light.
- Keep the scene under {max_nodes} nodes. Make it look good: color, composition, a little motion when it suits the request.
"""

VERSION_RULES = {
    "4.0": "EnvironmentLight, GaussianSplats, HAnimPose and InlineGeometry are X3D 4.1 nodes and must not appear.",
    "4.1": "X3D 4.1 adds EnvironmentLight and GaussianSplats; declare version 4.1 only because you use them.",
}

REPAIR = """The validator checked your scene and found these defects (exact messages, with line numbers in your document):

{defects}

Fix every defect listed and change nothing else that works. Return the whole corrected X3D document, not a diff, and no prose."""

REPAIR_VISIBLE = """The validator passes your scene, but a browser rendered it and this is what it saw:

{visibility}

Valid is not the same as visible. Fix the scene so the content is clearly in view (check the Viewpoint position and orientation against where the geometry is, transparency, scale, lights, and anything hidden). Keep it valid. Return the whole X3D document, no prose."""

SYSTEM_JUDGE = """You review X3D documents. You cannot run any tool. Decide whether the document is valid X3D for its declared version: well-formed XML, schema-valid (only real nodes and fields of that version, in allowed places, with well-typed values), and semantically consistent (DEF and USE, ROUTE endpoints exist and match in field type and direction, containerField, profile and component statements cover the nodes, interpolator key and keyValue counts agree).

Answer with JSON only:
{"verdict": "valid" | "invalid", "defects": [{"line": <number or null>, "what": "<short>", "fix": "<short>"}]}
List every defect you are confident of. An empty list means you judge the document valid."""

JUDGE_USER = """X3D document (line numbers added on the left for reference; they are not part of the file):

{numbered}"""


def write_system(version: str, max_nodes: int = 200) -> str:
    v = version if version in VERSION_RULES else "4.0"
    return SYSTEM_WRITE.format(version=v, version_rule=VERSION_RULES[v], max_nodes=max_nodes)


def numbered(text: str) -> str:
    return "\n".join(f"{i:4d}  {line}" for i, line in enumerate(text.splitlines(), 1))
