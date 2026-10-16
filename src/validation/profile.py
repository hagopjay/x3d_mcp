"""Profile and component conformance check.

The XSD validates structure only: a document declaring profile="Interchange"
that uses Rectangle2D passes the schema, yet X_ITE and x3d-tidy refuse to load
it ("Node type 'Rectangle2D' does not match component/profile statements").
ISO/IEC 19775-1 clause 7.2.5 requires the header to declare a profile, plus a
component statement for every component the scene uses beyond that profile.

Node membership per profile comes from the X3DUOM profileNameChoices appinfo
("Allowed X3D nodes for this profile are: ..."); each node's component and
level come from its X3DUOM componentInfo. Both are bundled, so no table is
hand-maintained here.
"""

import re
from dataclasses import dataclass, field
from lxml import etree

from x3d_utils.x3duom import get_x3duom

# Statements that are allowed in every profile (X3DUOM Core profile appinfo).
_STATEMENTS = {
    "connect", "ExternProtoDeclare", "EXPORT", "field", "fieldValue", "IMPORT",
    "IS", "ProtoBody", "ProtoDeclare", "ProtoInterface", "ProtoInstance",
    "ROUTE", "X3D", "Scene", "head", "meta", "component", "unit",
}

_profile_nodes: dict[str, set[str] | None] | None = None


@dataclass
class ProfileReport:
    profile: str | None
    components: dict[str, int]
    missing: list[dict] = field(default_factory=list)   # nodes not covered
    unknown: list[str] = field(default_factory=list)    # tags not in X3DUOM

    @property
    def ok(self) -> bool:
        return not self.missing


def profile_nodes() -> dict[str, set[str] | None]:
    """Return profile_name -> set of allowed node names (None means all nodes)."""
    global _profile_nodes
    if _profile_nodes is not None:
        return _profile_nodes
    result: dict[str, set[str] | None] = {}
    for name, info in get_x3duom().get_profiles().items():
        appinfo = info.get("appinfo", "")
        m = re.search(r"Allowed X3D nodes for this profile are:\s*(.*?)\.?\s*$", appinfo, re.S)
        if m:
            result[name] = set(m.group(1).split())
        else:
            # Full: "All X3D nodes and statements are allowed in this profile."
            result[name] = None
    _profile_nodes = result
    return result


def _local_tag(el: etree._Element) -> str:
    tag = el.tag if isinstance(el.tag, str) else ""
    return tag.split("}", 1)[1] if "}" in tag else tag


def declared_components(root: etree._Element) -> dict[str, int]:
    """Return {component name: level} from the head's component statements."""
    comps: dict[str, int] = {}
    for el in root.iter():
        if _local_tag(el) != "component":
            continue
        name = el.get("name")
        if not name:
            continue
        try:
            level = int(el.get("level", "1"))
        except ValueError:
            level = 1
        comps[name] = max(level, comps.get(name, 0))
    return comps


def check_profile(root: etree._Element) -> ProfileReport:
    """Check every node in the scene against the declared profile and components."""
    uom = get_x3duom()
    nodes = uom.get_concrete_nodes()
    profile = root.get("profile")
    comps = declared_components(root)
    report = ProfileReport(profile=profile, components=comps)

    allowed = profile_nodes().get(profile) if profile else set()
    if allowed is None:          # Full profile, or an unknown profile name
        return report

    scene = None
    for el in root.iter():
        if _local_tag(el) == "Scene":
            scene = el
            break
    if scene is None:
        return report

    seen: set[str] = set()
    for el in scene.iter():
        tag = _local_tag(el)
        if not tag or tag in seen or tag in _STATEMENTS:
            continue
        seen.add(tag)
        info = nodes.get(tag)
        if info is None:
            report.unknown.append(tag)
            continue
        if tag in allowed:
            continue
        comp, level = info.get("component"), info.get("level") or 1
        if comp and comps.get(comp, 0) >= level:
            continue
        report.missing.append({
            "node": tag,
            "component": comp,
            "level": level,
            "declared_level": comps.get(comp, 0),
            "line": el.sourceline,
        })
    report.missing.sort(key=lambda m: (m["component"] or "", m["level"], m["node"]))
    return report


def component_statements(report: ProfileReport) -> list[str]:
    """Return the <component .../> lines that would make the document conformant."""
    needed: dict[str, int] = {}
    for m in report.missing:
        if m["component"]:
            needed[m["component"]] = max(needed.get(m["component"], 0), m["level"])
    return [f"<component name='{c}' level='{lvl}'/>" for c, lvl in sorted(needed.items())]
