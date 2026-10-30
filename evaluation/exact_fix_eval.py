"""Measure the exact-fix hint layer on a seeded corpus of broken scenes (no model).

1. Seeds: the valid scenes the repository ships (examples/scenes, output/, the
   JSON fixtures converted with json2xml) plus scenes from dataset/generators
   at a fixed seed, each passed once through repair_x3d so it clears the gate
   (XSD plus error-level semantic checks). A seed that still fails is dropped.
2. Mutations, one per case, enumerated over every site in every seed and then
   sampled with random.Random(SEED): wrong parent, wrong containerField, bad
   enumeration value, missing required attribute (version, profile, ROUTE
   fields), out-of-range colour, misplaced ROUTE, ROUTE field typo, unknown
   attribute, unknown node name, bad value. A mutation the gate does not
   catch is counted and dropped.
3. For each case: hint coverage (errors that get a hint with a suggested fix),
   single-fix correctness (apply one hint's fix: does that error go away, and
   is the scene then valid), and the repair rate with repair_x3d alone
   (before), with repair_x3d then the hint loop (after), and with the hint loop
   alone.

    uv run python evaluation/exact_fix_eval.py [--seed 20261029] [--per-class 45]

writes evaluation/exact_fix/corpus.jsonl and evaluation/exact_fix/results.json.
"""

import argparse
import collections
import copy
import glob
import json
import random
import re
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from lxml import etree  # noqa: E402

from validation.hints import _model, apply_fix, gate, repair_with_hints  # noqa: E402
from validation.repair import repair_x3d  # noqa: E402
from validation.validate import validate_xml  # noqa: E402

OUT = ROOT / "evaluation" / "exact_fix"
COLOUR_TYPES = {"SFColor", "MFColor", "SFColorRGBA", "MFColorRGBA"}


def _local(el):
    t = el.tag if isinstance(el.tag, str) else ""
    return t.split("}", 1)[1] if "}" in t else t


def _ser(root) -> str:
    return '<?xml version="1.0" encoding="UTF-8"?>\n' + etree.tostring(root.getroottree(), encoding="unicode")


def _parse(text: str):
    return etree.fromstring(text.encode("utf-8"), etree.XMLParser(remove_blank_text=False, huge_tree=True))


def _canon(text: str) -> str:
    root = etree.fromstring(text.encode("utf-8"), etree.XMLParser(remove_blank_text=True))
    for el in root.iter():
        if isinstance(el.tag, str):
            el.text = el.text.strip() if el.text and el.text.strip() else None
            el.tail = None
    return etree.tostring(root, encoding="unicode")


# ---------------------------------------------------------------------------
# seeds


def load_seeds(seed: int) -> dict[str, str]:
    raw: dict[str, str] = {}
    for p in sorted(glob.glob(str(ROOT / "examples/scenes/*.x3d")) + glob.glob(str(ROOT / "output/*.x3d"))):
        raw[Path(p).relative_to(ROOT).as_posix()] = Path(p).read_text()
    from x3d_utils.json2xml import json_to_xml
    for p in sorted(glob.glob(str(ROOT / "tests/fixtures/json/*.x3dj"))) + [str(ROOT / "tests/fixtures/HelloWorld.json")]:
        raw[Path(p).relative_to(ROOT).as_posix()] = json_to_xml(Path(p).read_text())
    from dataset.generators.numeric_sequences import (generate_extrusion, generate_indexed_face_set,
                                                      generate_interpolator)
    rng = random.Random(seed)
    for name, fn, n in (("interpolator", generate_interpolator, 10), ("indexed-face-set", generate_indexed_face_set, 3),
                        ("extrusion", generate_extrusion, 3)):
        for i in range(n):
            raw[f"dataset/generators:{name}:{i}"] = fn("short", rng).output
    seeds = {}
    for k, t in raw.items():
        if not gate(t)["valid"]:
            r = repair_x3d(t)
            t = r.get("fixed") or t
        if gate(t)["valid"]:
            seeds[k] = t
    return seeds


# ---------------------------------------------------------------------------
# mutation sites: each yields (kind, fn(root) -> description) closures over positions

def _elements(root):
    return [e for e in root.iter() if isinstance(e.tag, str)]


def _idx(root, el):
    return _elements(root).index(el)


def _at(root, i):
    return _elements(root)[i]


def sites(name: str, text: str, m) -> list[tuple[str, str, dict]]:
    """Every (class, subkind, params) mutation site in one seed."""
    root = _parse(text)
    els = _elements(root)
    out = []
    nodes = m.nodes

    def cf(el):
        return el.get("containerField") or nodes.get(_local(el), {}).get("containerField")

    for i, el in enumerate(els):
        tag = _local(el)
        par = el.getparent()
        ptag = _local(par) if par is not None else ""
        # wrong parent
        if tag == "Shape":
            for c in el:
                if isinstance(c.tag, str) and cf(c) == "geometry" and not c.get("USE"):
                    out.append(("wrong-parent", "geometry-out-of-shape", {"el": _idx(root, c)}))
        if tag == "Appearance" and ptag == "Shape":
            for c in el:
                if isinstance(c.tag, str) and _local(c) in nodes:
                    out.append(("wrong-parent", "appearance-child-into-shape", {"el": _idx(root, c)}))
        if tag in nodes and cf(el) == "children" and ptag in ("Scene", "Transform", "Group"):
            shapes = [s for s in root.iter("Shape") if el not in set(s.iterancestors()) and s is not el]
            for s in shapes[:2]:
                out.append(("wrong-parent", "child-node-into-shape", {"el": i, "to": _idx(root, s)}))
        # wrong containerField
        if tag in nodes and ptag in nodes and cf(el) and not el.get("USE"):
            c = cf(el)
            out.append(("wrong-field", "containerField-typo", {"el": i, "value": c[:-1] if len(c) > 3 else c + "s"}))
            others = [f for f in m.node_fields(ptag) if f != c and not m.accepts(ptag, f, tag)]
            if others:
                out.append(("wrong-field", "containerField-other-field", {"el": i, "value": sorted(others)[0]}))
        for a, v in el.attrib.items():
            if a in ("DEF", "USE", "containerField") or "}" in a:
                continue
            f = m.field(tag, a)
            ftype = f.get("type") if f else None
            # enumeration and value checks are found by trial below
            out.append(("probe", "enum", {"el": i, "attr": a, "value": v.lower() if v.lower() != v else v[:-1] if len(v) > 2 else v + "x"}))
            out.append(("missing-required", "remove-attribute", {"el": i, "attr": a}))
            if ftype in COLOUR_TYPES:
                nums = [float(x) for x in re.split(r"[\s,]+", v.strip()) if x]
                out.append(("colour", "0-255-scale", {"el": i, "attr": a, "value": " ".join(str(round(x * 255)) for x in nums)}))
                if nums:
                    bumped = list(nums)
                    bumped[0] = round(bumped[0] + 1.0, 3)
                    out.append(("colour", "component-above-1", {"el": i, "attr": a, "value": " ".join(f"{x:g}" for x in bumped)}))
                    neg = list(nums)
                    neg[-1] = -0.2
                    out.append(("colour", "negative-component", {"el": i, "attr": a, "value": " ".join(f"{x:g}" for x in neg)}))
            if ftype == "SFBool":
                out.append(("bad-value", "bool-case", {"el": i, "attr": a, "value": v.upper()}))
            if ftype in ("SFFloat", "SFTime", "SFDouble", "SFInt32") and f.get("minInclusive") is not None:
                out.append(("bad-value", "below-minimum", {"el": i, "attr": a, "value": "-1"}))
            if ftype in ("SFVec3f", "SFVec2f", "SFRotation") and tag in nodes:
                out.append(("bad-value", "comma-separated", {"el": i, "attr": a, "value": ", ".join(v.split())}))
            if ftype and ftype not in COLOUR_TYPES and tag in nodes and len(a) > 4:
                out.append(("unknown-attribute", "misspelled", {"el": i, "attr": a, "to": a.replace("Color", "Colour") if "Color" in a else a[:-1]}))
        if tag == "Material":
            for a in ("roughness", "metalness", "metallic", "opacity"):
                out.append(("unknown-attribute", "pbr-on-material", {"el": i, "attr": a, "value": "0.5"}))
        if tag in nodes and tag not in ("ProtoInstance",) and not el.get("USE") and ptag != "":
            out.append(("unknown-node", "lowercase", {"el": i, "to": tag.lower()}))
            syn = {"Box": "Cube", "Sphere": "Ball", "Cone": "Pyramid"}.get(tag)
            if syn:
                out.append(("unknown-node", "synonym", {"el": i, "to": syn}))
            if len(tag) > 5:
                out.append(("unknown-node", "typo", {"el": i, "to": tag[:2] + tag[3:]}))
        # stress: a scene node moved under a random other element the X3DUOM says cannot take it
        if tag in nodes and not el.get("USE") and (ptag in nodes or ptag == "Scene"):
            for j, other in enumerate(els):
                ot = _local(other)
                if (ot in nodes and other is not par and other is not el and el not in set(other.iterancestors())
                        and not m.fields_accepting(ot, tag)):
                    out.append(("random-relocation", "node-under-random-element", {"el": i, "to": j}))
        if tag == "ROUTE":
            for target in ("head", "Shape", "Appearance", "Material"):
                hits = [e for e in root.iter(target)]
                if hits:
                    out.append(("misplaced-route", f"route-into-{target}", {"el": i, "to": _idx(root, hits[0])}))
            for a in ("fromField", "toField"):
                v = el.get(a, "")
                if len(v) > 4:
                    out.append(("route-field", f"{a}-typo", {"el": i, "attr": a, "value": v[:-2]}))
    if root.get("version"):
        out.append(("missing-required", "no-version", {"el": 0, "attr": "version"}))
    if root.get("profile"):
        out.append(("missing-required", "no-profile", {"el": 0, "attr": "profile"}))
    return out


def mutate(text: str, kind: str, sub: str, p: dict) -> str | None:
    root = _parse(text)
    el = _at(root, p["el"])
    if kind == "wrong-parent":
        par = el.getparent()
        if sub == "geometry-out-of-shape":
            par.remove(el)
            par.addnext(el)
        elif sub == "appearance-child-into-shape":
            shape = par.getparent()
            par.remove(el)
            shape.append(el)
        else:
            to = _at(root, p["to"])
            par.remove(el)
            to.append(el)
    elif kind == "random-relocation":
        to = _at(root, p["to"])
        el.getparent().remove(el)
        to.append(el)
    elif kind in ("wrong-field",):
        el.set("containerField", p["value"])
    elif kind in ("probe", "colour", "bad-value", "route-field"):
        el.set(p["attr"], p["value"])
    elif kind == "missing-required":
        if el.get(p["attr"]) is None:
            return None
        del el.attrib[p["attr"]]
    elif kind == "unknown-attribute":
        if sub == "pbr-on-material":
            el.set(p["attr"], p["value"])
        else:
            v = el.attrib.pop(p["attr"])
            el.set(p["to"], v)
    elif kind == "unknown-node":
        el.tag = p["to"]
    elif kind == "misplaced-route":
        to = _at(root, p["to"])
        el.getparent().remove(el)
        to.append(el)
    return _ser(root)


def build_corpus(seeds: dict[str, str], seed: int, per_class: int):
    m = _model("4.1")
    all_sites = []
    for name, text in seeds.items():
        mv = _model("4.0" if _parse(text).get("version") == "4.0" else "4.1")
        for kind, sub, p in sites(name, text, mv):
            all_sites.append((name, kind, sub, p))
    by_class = collections.defaultdict(list)
    dropped = collections.Counter()
    for name, kind, sub, p in all_sites:
        by_class[kind].append((name, sub, p))
    rng = random.Random(seed)
    cases = []
    for kind in sorted(by_class):
        pool = by_class[kind]
        rng.shuffle(pool)
        taken = 0
        for name, sub, p in pool:
            if taken >= per_class and kind != "probe":
                break
            broken = mutate(seeds[name], kind, sub, p)
            if broken is None:
                dropped[(kind, "no-site")] += 1
                continue
            g = gate(broken)
            trial = kind == "probe" or (kind == "missing-required" and sub == "remove-attribute")
            if g["valid"]:
                if not trial:           # a trial site that passes is just not that kind of site
                    dropped[(kind, f"{sub}:not-caught")] += 1
                continue
            real_kind = kind
            if kind == "probe":         # an attribute value change: keep it as bad-enum only if the XSD says enumeration
                if "[facet 'enumeration']" not in (g["errors"][0] if g["errors"] else ""):
                    continue
                real_kind = "bad-enum"
                if sum(1 for c in cases if c["class"] == "bad-enum") >= per_class:
                    continue
            if kind == "missing-required" and sub == "remove-attribute" and "is required but missing" not in g["errors"][0]:
                continue
            cases.append({"id": f"case-{len(cases):04d}", "class": real_kind, "subkind": sub, "seed": name,
                          "params": p, "broken": broken})
            taken += 1
    # two faults in one scene: two single-fault cases on the same seed, both applied
    singles = [c for c in cases if c["class"] not in ("random-relocation",)]
    by_seed = collections.defaultdict(list)
    for c in singles:
        by_seed[c["seed"]].append(c)
    pairs = [(a, b) for cs in by_seed.values() for a in cs for b in cs
             if a["class"] < b["class"] and a["class"] != "probe"]
    rng.shuffle(pairs)
    taken = 0
    for a, b in pairs:
        if taken >= per_class:
            break
        kind_a = "probe" if a["class"] == "bad-enum" else a["class"]
        kind_b = "probe" if b["class"] == "bad-enum" else b["class"]
        first = mutate(seeds[a["seed"]], kind_a, a["subkind"], a["params"])
        if first is None or len(_elements(_parse(first))) != len(_elements(_parse(seeds[a["seed"]]))):
            continue
        try:
            both = mutate(first, kind_b, b["subkind"], b["params"])
        except (IndexError, KeyError, AttributeError, TypeError):
            continue
        if both is None or gate(both)["valid"]:
            continue
        cases.append({"id": f"case-{len(cases):04d}", "class": "two-faults",
                      "subkind": f"{a['class']}+{b['class']}", "seed": a["seed"],
                      "params": {"a": a["params"], "b": b["params"]}, "broken": both})
        taken += 1
    return cases, dropped, len(all_sites)


# ---------------------------------------------------------------------------
# measurement


def _msg(e: str) -> str:
    return re.sub(r"^Line \d+: ", "", e)


def measure(cases, seeds):
    per_case = []
    for c in cases:
        broken = c["broken"]
        g = gate(broken)
        errs = g["errors"]
        hints = g["hints"]
        by_err = collections.defaultdict(list)
        for h in hints:
            if "error_index" in h:
                by_err[h["error_index"]].append(h)
        with_fix = sum(1 for i in range(len(errs)) if any(h.get("suggested_fix") for h in by_err.get(i, [])))
        with_hint = sum(1 for i in range(len(errs)) if by_err.get(i))
        single = []
        for h in hints:
            fx = h.get("suggested_fix")
            if not fx:
                continue
            try:
                new = apply_fix(broken, fx)
            except Exception as exc:     # noqa: BLE001 - counted as a failed fix
                single.append({"rule": h.get("rule"), "applied": False, "resolves": False, "valid": False,
                               "why": str(exc)[:80]})
                continue
            g2 = gate(new)
            target = errs[h["error_index"]] if h.get("error_index") is not None and h["error_index"] < len(errs) else None
            before_n = sum(1 for e in errs if _msg(e) == _msg(target)) if target else 0
            after_n = sum(1 for e in g2["errors"] if _msg(e) == _msg(target)) if target else 0
            resolves = bool(target) and (g2["valid"] or (g["stage"] == g2["stage"] and after_n < before_n)
                                         or (g["stage"] == "xsd" and g2["stage"] == "semantic"))
            single.append({"rule": h.get("rule"), "op": fx.get("op"), "applied": True, "resolves": resolves,
                           "valid": g2["valid"]})
        r0 = repair_x3d(broken)
        before = r0.get("fixed") or broken
        before_valid = gate(before)["valid"]
        after = repair_with_hints(before)
        only = repair_with_hints(broken)
        seed_c = _canon(seeds[c["seed"]])
        per_case.append({
            "id": c["id"], "class": c["class"], "subkind": c["subkind"], "stage": g["stage"],
            "errors": len(errs), "errors_with_hint": with_hint, "errors_with_fix": with_fix,
            "rules": sorted({h.get("rule", "") for h in hints}),
            "single": single,
            "first_error": _msg(errs[0])[:160] if errs else "",
            "before_valid": before_valid, "after_valid": after["valid"], "hints_only_valid": only["valid"],
            "hints_only_steps": len(only["applied"]),
            "hints_only_ops": [a["fix"].get("op") for a in only["applied"]],
            "hints_only_restores_seed": only["valid"] and _canon(only["fixed"]) == seed_c,
            "after_remaining": [_msg(e)[:160] for e in after["remaining_errors"][:2]],
        })
    return per_case


def summarize(per_case):
    def rate(n, d):
        return {"n": n, "of": d, "pct": round(100 * n / d, 1) if d else None}

    def block(rows):
        errs = sum(r["errors"] for r in rows)
        singles = [s for r in rows for s in r["single"]]
        return {
            "cases": len(rows),
            "errors": errs,
            "hint_coverage_any": rate(sum(r["errors_with_hint"] for r in rows), errs),
            "hint_coverage_with_fix": rate(sum(r["errors_with_fix"] for r in rows), errs),
            "fixes_suggested": len(singles),
            "fix_resolves_its_error": rate(sum(s["resolves"] for s in singles), len(singles)),
            "fix_alone_makes_valid": rate(sum(s["valid"] for s in singles), len(singles)),
            "repair_before_repair_x3d": rate(sum(r["before_valid"] for r in rows), len(rows)),
            "repair_after_repair_x3d_then_hints": rate(sum(r["after_valid"] for r in rows), len(rows)),
            "repair_hints_only": rate(sum(r["hints_only_valid"] for r in rows), len(rows)),
            "hints_only_restores_seed_exactly": rate(sum(r["hints_only_restores_seed"] for r in rows), len(rows)),
            "hints_only_valid_with_an_element_removed": rate(
                sum(r["hints_only_valid"] and "remove_element" in r["hints_only_ops"] for r in rows), len(rows)),
            "fix_ops": dict(collections.Counter(s.get("op") for s in singles).most_common()),
        }
    out = {"all": block(per_case), "by_class": {}}
    for k in sorted({r["class"] for r in per_case}):
        out["by_class"][k] = block([r for r in per_case if r["class"] == k])
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--seed", type=int, default=20261029)
    ap.add_argument("--per-class", type=int, default=45)
    a = ap.parse_args()
    t0 = time.time()
    seeds = load_seeds(a.seed)
    cases, dropped, n_sites = build_corpus(seeds, a.seed, a.per_class)
    per_case = measure(cases, seeds)
    summary = summarize(per_case)
    OUT.mkdir(parents=True, exist_ok=True)
    with (OUT / "corpus.jsonl").open("w") as f:
        for c in cases:
            f.write(json.dumps(c, sort_keys=True) + "\n")
    res = {"seed": a.seed, "per_class": a.per_class, "seeds": sorted(seeds), "sites": n_sites,
           "dropped": {f"{k[0]}:{k[1]}": v for k, v in sorted(dropped.items())},
           "seconds": round(time.time() - t0, 1), "summary": summary, "cases": per_case}
    (OUT / "results.json").write_text(json.dumps(res, indent=1, sort_keys=True) + "\n")
    s = summary["all"]
    print(f"seeds {len(seeds)}, sites {n_sites}, cases {s['cases']}, errors {s['errors']}, {res['seconds']}s")
    for k, v in s.items():
        if isinstance(v, dict) and "n" in v:
            print(f"  {k:40s} {v['n']}/{v['of']} = {v['pct']}%")
    print("by class (coverage-with-fix | fix resolves | before | after | hints-only):")
    for k, b in summary["by_class"].items():
        print(f"  {k:18s} n={b['cases']:3d}  {b['hint_coverage_with_fix']['pct']}% | {b['fix_resolves_its_error']['pct']}% | "
              f"{b['repair_before_repair_x3d']['pct']}% | {b['repair_after_repair_x3d_then_hints']['pct']}% | "
              f"{b['repair_hints_only']['pct']}% | removed-element {b['hints_only_valid_with_an_element_removed']['pct']}% | "
              f"restores seed {b['hints_only_restores_seed_exactly']['pct']}%")
    print("dropped:", res["dropped"])
    print("fix ops:", s["fix_ops"])


if __name__ == "__main__":
    main()
