"""Reorder sibling nodes so every USE follows the DEF it references.

x3d.py writes a RigidBodyCollection's fields in the order joints, collider,
bodies, so every joint's ``body1``/``body2`` USE and every ``collidables`` USE
precedes the RigidBody that DEFs it (ISO/IEC 19775-1 requires DEF first).

The fix only permutes the children of one parent, and only across different
containerFields -- the order of nodes inside one field can matter (a Switch's
``children``), the order between fields never does. Elements that are not
nodes (ROUTE, IMPORT, EXPORT, ...) stay where they are and split the children
into runs that are sorted independently. A USE and DEF that cannot be fixed
this way (the same field, a cycle, the USE inside the DEF's own subtree) are
left for the semantic check to report.
"""

import heapq

from lxml import etree

from x3d_utils.x3duom import get_x3duom
from validation.semantic import _local_tag


def _defs_and_uses(el: etree._Element) -> tuple[set[str], set[str]]:
    defs: set[str] = set()
    uses: set[str] = set()
    for e in el.iter():
        if e.get("DEF"):
            defs.add(e.get("DEF"))
        if e.get("USE"):
            uses.add(e.get("USE"))
    return defs, uses


def _sort_run(run: list[etree._Element], nodes: dict) -> list[etree._Element] | None:
    """Stable topological order of one run of sibling nodes; None if cyclic."""
    n = len(run)
    info = [_defs_and_uses(el) for el in run]
    defined_in = {name: i for i, (defs, _) in enumerate(info) for name in defs}
    after: list[set[int]] = [set() for _ in range(n)]     # edges i -> j: i before j
    last_in_field: dict[str, int] = {}
    for i, el in enumerate(run):
        cf = el.get("containerField") or nodes[_local_tag(el)]["containerField"]
        if cf in last_in_field:                           # same field keeps its order
            after[last_in_field[cf]].add(i)
        last_in_field[cf] = i
        for name in info[i][1]:
            j = defined_in.get(name)
            if j is not None and j != i:
                after[j].add(i)
    indegree = [0] * n
    for succ in after:
        for j in succ:
            indegree[j] += 1
    ready = [i for i in range(n) if indegree[i] == 0]
    heapq.heapify(ready)
    order: list[int] = []
    while ready:
        i = heapq.heappop(ready)                          # lowest index: least movement
        order.append(i)
        for j in after[i]:
            indegree[j] -= 1
            if indegree[j] == 0:
                heapq.heappush(ready, j)
    return [run[i] for i in order] if len(order) == n else None


def reorder_defs(root: etree._Element) -> list[dict]:
    """Move DEFs ahead of the USEs that reference them; return what moved.

    Each change is {parent, node, ref, from, to}: ``node`` moved from child
    index ``from`` to ``to`` under ``parent``, ``ref`` is its DEF name or "".
    """
    nodes = get_x3duom().get_concrete_nodes()
    changes: list[dict] = []
    for parent in list(root.iter()):
        kids = [c for c in parent if isinstance(c.tag, str)]
        if len(kids) < 2:
            continue
        runs: list[list[etree._Element]] = [[]]
        for c in kids:
            if _local_tag(c) in nodes:
                runs[-1].append(c)
            else:
                runs.append([])                           # ROUTE etc. split the runs
        for run in runs:
            if len(run) < 2:
                continue
            ordered = _sort_run(run, nodes)
            if ordered is None or ordered == run:
                continue
            before = {id(el): i for i, el in enumerate(run)}
            for i, el in enumerate(ordered):
                if before[id(el)] != i:
                    changes.append({
                        "parent": _local_tag(parent), "node": _local_tag(el),
                        "ref": el.get("DEF") or "", "from": before[id(el)], "to": i,
                    })
            members = {id(el) for el in run}
            children = list(parent)
            positions = [i for i, c in enumerate(children) if id(c) in members]
            for el in run:
                parent.remove(el)
            for pos, el in zip(positions, ordered):
                parent.insert(pos, el)
    return changes


def reorder_defs_xml(xml_string: str) -> str:
    """reorder_defs on a document string; the string itself when nothing moves."""
    tree = etree.ElementTree(etree.fromstring(
        xml_string.encode("utf-8"), etree.XMLParser(remove_blank_text=True)))
    if not reorder_defs(tree.getroot()):
        return xml_string
    return ('<?xml version="1.0" encoding="UTF-8"?>\n'
            + etree.tostring(tree, pretty_print=True, encoding="unicode"))
