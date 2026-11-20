"""Where a node may be placed, taken from the X3DUOM (the table behind the where_can_place tool).

The ontology's containment lattice (node_parents) is looser than the X3DUOM: it admits node types the XSD
rejects and misses some it accepts. This table is built from the X3DUOM's acceptableNodeTypes on every SFNode
and MFNode field, so each row names a real parent node, the field that holds the child, and the
containerField the child needs when it is not that field's default. Rows are sorted, so a page always has the
same bytes and the same hash.

The X3DUOM and the XSD do not always agree. `verified-containment.json` holds the XSD's verdict for every
X3DUOM-allowed (parent, field, child), built by evaluation/build_verified_containment.py: each answered row
carries it as `xsd` ("A" the XSD accepts a minimal document with that placement, "R" it rejects it, "I" the
probe was inconclusive). The verdict is added to the answer, not to the table, so table_sha256 is unchanged.
"""

import hashlib
import json
import re

from pathlib import Path

from x3d_utils.x3duom import X3DUOM_PATH, get_x3duom

SOURCE = "X3DUOM"
_NODE_FIELD_TYPES = ("SFNode", "MFNode")

_table = None


def _ancestors(name: str, concrete: dict, abstract: dict) -> set[str]:
    """The node's own name plus every abstract type it derives from."""
    seen: set[str] = set()
    stack = [name]
    while stack:
        cur = stack.pop()
        if cur in seen:
            continue
        seen.add(cur)
        info = concrete.get(cur) or abstract.get(cur) or {}
        if info.get("baseType"):
            stack.append(info["baseType"])
        stack.extend(info.get("additionalBaseTypes") or [])
    return seen


def _build() -> dict:
    uom = get_x3duom()
    concrete = uom.get_concrete_nodes()
    abstract = uom.get_abstract_types()
    by_child: dict[str, list[dict]] = {name: [] for name in concrete}
    ancestry = {name: _ancestors(name, concrete, abstract) for name in concrete}
    for parent in sorted(concrete):
        for f in concrete[parent]["fields"]:
            if f["type"] not in _NODE_FIELD_TYPES or f.get("accessType") in ("inputOnly", "outputOnly"):
                continue                    # addChildren and removeChildren carry events, they hold nothing
            accepted = {t for t in re.split(r"[|\s]+", f.get("acceptableNodeTypes") or "") if t}   # space or pipe separated
            accepted.discard("IS")
            if not accepted:
                continue
            for child, anc in ancestry.items():
                if anc & accepted:
                    default = concrete[child]["containerField"]
                    by_child[child].append({
                        "parent": parent,
                        "field": f["name"],
                        "fieldType": f["type"],
                        "needsContainerField": default != f["name"],
                    })
    for rows in by_child.values():
        rows.sort(key=lambda r: (r["parent"], r["field"]))
    for child, rows in by_child.items():
        default = concrete[child]["containerField"]
        for r in rows:
            r["containerField"] = r["field"] if r["needsContainerField"] else default
    canon = json.dumps(by_child, sort_keys=True, separators=(",", ":"))
    return {
        "source": SOURCE,
        "rows": by_child,
        "table_sha256": hashlib.sha256(canon.encode()).hexdigest(),
        "row_count": sum(len(v) for v in by_child.values()),
    }


def get_table() -> dict:
    global _table
    if _table is None:
        _table = _build()
    return _table


VERIFIED_PATH = Path(__file__).resolve().parent.parent / "validation" / "schemas" / "verified-containment.json"
_verified = None


def _xsd_exceptions() -> dict | None:
    """{"Parent.field": {"R": [...], "I": [...]}} for the X3DUOM version the table is built from, or None when
    the verdict file is absent or does not cover that version."""
    global _verified
    if _verified is None:
        m = re.search(r"(\d+\.\d+)\.xml$", str(X3DUOM_PATH))
        try:
            versions = json.loads(VERIFIED_PATH.read_text(encoding="utf-8"))["versions"]
            _verified = (m.group(1), versions[m.group(1)]["exceptions"]) if m and m.group(1) in versions else (None, None)
        except (OSError, ValueError, KeyError):
            _verified = (None, None)
    return _verified[1]


def xsd_verdict(parent: str, field: str, child: str) -> str | None:
    """"A", "R" or "I" for one placement; None when no verdict table covers this X3DUOM version."""
    exc = _xsd_exceptions()
    if exc is None:
        return None
    e = exc.get(f"{parent}.{field}", {})
    return "R" if child in e.get("R", ()) else "I" if child in e.get("I", ()) else "A"


def where_can_place(child: str, offset: int = 0, limit: int = 50, verified_only: bool = False) -> dict:
    """One page of the places `child` may go, each with the XSD's verdict. verified_only keeps the rows the
    XSD accepts. Raises KeyError for an unknown node type."""
    table = get_table()
    rows = table["rows"].get(child)
    if rows is None:
        raise KeyError(child)
    rows = [{**r, "xsd": xsd_verdict(r["parent"], r["field"], child)} for r in rows]
    counts = {v: sum(r["xsd"] == v for r in rows) for v in ("A", "R", "I")}
    if verified_only:
        rows = [r for r in rows if r["xsd"] == "A"]
    offset = max(0, int(offset))
    limit = max(1, min(int(limit), 500))
    page = rows[offset:offset + limit]
    nxt = offset + limit
    return {
        "child": child,
        "source": table["source"],
        "table_sha256": table["table_sha256"],
        "total": len(rows),
        "offset": offset,
        "rows": page,
        "next_offset": nxt if nxt < len(rows) else None,
        "xsd": {"source": "verified-containment.json" if _xsd_exceptions() is not None else None, "version": _verified[0],
                "accepted": counts["A"], "rejected": counts["R"], "inconclusive": counts["I"], "verified_only": bool(verified_only)},
    }
