"""Where a node may be placed, taken from the X3DUOM (the table behind the where_can_place tool).

The ontology's containment lattice (node_parents) is looser than the X3DUOM: it admits node types the XSD
rejects and misses some it accepts. This table is built from the X3DUOM's acceptableNodeTypes on every SFNode
and MFNode field, so each row names a real parent node, the field that holds the child, and the
containerField the child needs when it is not that field's default. Rows are sorted, so a page always has the
same bytes and the same hash.
"""

import hashlib
import json
import re

from x3d_utils.x3duom import get_x3duom

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


def where_can_place(child: str, offset: int = 0, limit: int = 50) -> dict:
    """One page of the places `child` may go. Raises KeyError for an unknown node type."""
    table = get_table()
    rows = table["rows"].get(child)
    if rows is None:
        raise KeyError(child)
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
    }
