# Exact-fix hints

Grades: **C** confirmed by a tool run in this repository, **L** likely, **S** suspected (not yet measured).

## What it is

The XSD tells you what is wrong but not how to fix it. `Element 'Box': This element is not expected.` gives a model in a repair loop no list of the places a Box may go. `src/validation/hints.py` turns each validator error into a structured hint:

```json
{"rule": "wrong-parent", "source": "xsd", "error_index": 0,
 "node": "Box", "parent": "Transform", "field": "geometry", "line": 3,
 "path": "/X3D/Scene/Transform/Box",
 "allowed_parents": ["CADFace.shape", "...", "Shape.geometry", "..."],
 "message": "Transform has no field that takes a Box. Wrap it: <Shape><Box/></Shape> (Shape.geometry takes a Box).",
 "suggested_fix": {"op": "wrap", "path": "/X3D/Scene/Transform/Box", "wrappers": ["Shape"]},
 "confidence": "choice"}
```

The hints come from data the server already ships, plus one new table:

- X3DUOM: `acceptableNodeTypes` for each parent field, read through the child's abstract ancestors. Also field types, defaults and `minInclusive`.
- `src/validation/schemas/verified-containment.json`, a new file of 154 KB built by `evaluation/build_verified_containment.py`. For every (parent, field, child) triple that X3DUOM allows, the script places the child in a minimal document and runs the bundled XSD. A placement is suggested only if the XSD accepted it (verdict A). **C**:
  - X3D 4.0: 12,634 allowed triples, 8,311 accepted, 2,738 rejected, 1,585 inconclusive.
  - X3D 4.1: 13,017 allowed triples, 8,561 accepted, 2,818 rejected, 1,638 inconclusive.
  - 1,550 of the 4.1 rejections (1,514 in 4.0) are `addChildren` and `removeChildren`. These are inputOnly fields, so the XSD's containerField enumeration does not offer them.
  - This count is wider than the 4,570 rejections in the x3d.onto ledger. That run tested the triples the ontology predicts; this one tests every triple X3DUOM allows.
- The XSD message itself: the expected-element list, the enumeration set, the facet bound and the pattern.

### Rules covered

| Kind | Rules |
|---|---|
| Element placement | `wrong-parent` (fix by moving into a sibling with a free slot, wrapping in Shape and/or Appearance, or moving up to the nearest ancestor that takes the node; deleting the element is the last resort), `wrong-containerfield`, `sfnode-occupied`, `unknown-node` (close name, plus a small synonym table: Cube→Box, Ball→Sphere, ...), `misplaced-route`, `misplaced-statement`, `missing-child` |
| Attributes | `bad-enum`, `colour-out-of-range` (0..255 values are divided by 255, others are clamped to 0..1), `bad-value` (SFBool case, commas, tuple size), `out-of-range`, `unknown-attribute` (rename to a close field, or remove it and say which related node has that field, e.g. PhysicalMaterial for `roughness`), `missing-attribute` (version: the lowest bundled version that has every node; profile: the smallest covering profile; ROUTE fields: inferred from the endpoint's field types) |
| Semantic, once the XSD passes | containerField checks, ROUTE endpoints and fields, `use-before-def` (swap the DEF and the first USE), `profile-component` (the component statements to add) |

## How to call it

Existing outputs and signatures are unchanged. The repository's compatibility tests pin both the `validate_xml` signature and the exact output and parameter lists of the existing MCP tools, so the hints are a new function and a new tool:

- `validate_xml_with_hints(xml, version=None)` in `validation.validate` returns what `validate_xml` returns, plus `hints` when the document is invalid. `validate_xml` itself is untouched.
- MCP tool `exact_fix_hints(content | path | blob_id, apply=False)` runs the XSD for the declared version and then, once that passes, the error-level semantic checks. It returns `{valid, stage, errors, hints}`. With `apply=true` it also applies the fixes one at a time, re-checking after each, and adds `{fixed, valid_after, applied, remaining_errors}`.
- In Python:
  - `validation.hints.gate(xml)` returns the errors and their hints for the same two-stage check.
  - `apply_fix(xml, fix)` applies one fix.
  - `repair_with_hints(xml)` applies hints one at a time and re-checks after each.
  - `semantic_hints(xml)` returns the semantic hints.

## Measured, with no model (all C)

`uv run python evaluation/exact_fix_eval.py` (seed 20261029, 45 cases per class) writes `evaluation/exact_fix/results.json`.

- **Seeds:** 25 scenes, all passing the gate (XSD plus error-level semantic checks):
  - the scenes in `examples/scenes` and `output/`;
  - the five JSON fixtures, converted with json2xml;
  - 16 scenes from `dataset/generators`, generated at the seed.
- **Mutations:** 15,651 mutation sites were enumerated and then sampled. A mutation the gate did not catch was dropped; that happened twice, both negative components in `skyColor` lists. One case has one mutation, except the `two-faults` class.
- **Result:** 540 cases with 570 errors.

| Measure | Result |
|---|---|
| Errors with any hint | 570 / 570 = 100% |
| Errors with a hint that has a `suggested_fix` (coverage) | 552 / 570 = 96.8% |
| A single fix, applied alone, removes the error it targets | 552 / 552 = 100% |
| A single fix, applied alone, makes the scene pass the gate | 478 / 552 = 86.6% (the remainder are mostly two-fault cases) |
| **Repair rate before: `repair_x3d` alone** | **81 / 540 = 15.0%** |
| **Repair rate after: `repair_x3d`, then the hint loop** | **523 / 540 = 96.9%** |
| Hint loop alone | 522 / 540 = 96.7% |
| Hint loop valid only because an element was deleted | 12 / 540 = 2.2% |
| Hint loop gives back the seed exactly (canonical XML) | 257 / 540 = 47.6% |

Before the hint layer, structured-hint coverage was 0% because the field did not exist. The before/after comparison that matters is the repair rate.

### By class

Columns are case counts out of n (errors in brackets when they differ):

- **fix**: errors that get a suggested fix
- **before**: valid after `repair_x3d` alone
- **after**: valid after `repair_x3d`, then the hint loop
- **deleted**: valid only because an element was deleted
- **seed**: the seed recovered exactly

| Class | n | fix | before | after | deleted | seed |
|---|---|---|---|---|---|---|
| wrong-parent (geometry out of Shape, Appearance child in Shape, node into Shape) | 45 | 45 | 0 | 45 | 0 | 17 |
| random-relocation (node under a random element X3DUOM says cannot take it) | 45 | 45 | 0 | 45 | 12 | 0 |
| wrong-field (containerField typo or wrong field) | 45 | 45 | 45 | 45 | 0 | 0 |
| bad-enum | 45 | 45 | 21 | 45 | 0 | 44 |
| missing-required (version, profile, ROUTE, meta, component attributes) | 45 | 28 | 15 | 29 | 0 | 2 |
| colour (0..255, a component above 1, a negative component) | 45 | 45 | 0 | 45 | 0 | 4 |
| misplaced-route (in head, Shape, Appearance, Material) | 45 | 45 | 0 | 45 | 0 | 23 |
| route-field (fromField or toField typo) | 45 | 45 | 0 | 45 | 0 | 45 |
| unknown-attribute (PBR fields on Material, misspellings) | 45 | 45 | 0 | 45 | 0 | 38 |
| unknown-node (lower case, synonym, typo) | 45 | 45 | 0 | 45 | 0 | 45 |
| bad-value (SFBool case, below minimum, commas) | 45 | 45 | 0 | 45 | 0 | 35 |
| two-faults (two of the above in one scene) | 45 (75) | 74 | 0 | 44 | 0 | 4 |

The 18 errors without a fix are `meta` without `name` or `content` (15), `component` without `name` or `level` (2), and one in a two-fault case. These values carry the author's intent and cannot be derived from tables, so the hint gives the rule and leaves the value to the author.

### How to read these numbers

- **Restoring validity is not restoring intent.** Colour fixes and moves of a ROUTE to the end of the Scene give valid scenes that differ from the seed. On wrong-field, `repair_x3d` and the hints both write an explicit `containerField` where the seed relied on the default, which is equivalent but not byte-equal. On random-relocation, 12 of 45 cases became valid only by deleting the stray node. Validity measures the gate; the "seed" column is a stricter proxy for intent. **C** for the counts; that the hint loop preserves intent in general is **L** at best.
- **Same author.** I wrote the mutation classes and the hint rules together, so the corpus measures the errors the hints were designed for. The random-relocation and two-faults classes are the least tailored. Real model errors are a different distribution: the zero-shot X3D failures in the eval-plan run were almost all invented `Material` PBR attributes, which `unknown-attribute` covers. A corpus of real model outputs would be a fairer test. **S** that these numbers carry over to it.
- **No trial validation.** The hint layer does not run the XSD on a candidate fix before suggesting it; it only checks the pattern facet for short values. The 100% "removes the error it targets" is therefore a measurement, not a construction. A fix can still reveal the next error in the same parent, because libxml2 reports one content error per parent.

## Model repair loop: not measured (S)

Whether hints improve a model's repair loop is **S** until a model run measures it. The eval-plan X3D run already reached 60/60 with plain error reports (gemini-3.8-flash, one round), so on those prompts the headroom is small. The more likely effects are fewer rounds, fewer tokens, and harder or two-fault scenes (all S).

The run is ready: `evaluation/hint_loop_gemini.py`.

- **Design:** paired, on the same corpus.
  - Arm E: the errors only.
  - Arm H: the errors plus each hint's message and exact edit.
  - Up to 2 rounds, cases stratified by class.
- **Reports:** validity after round 1 and at the end, paired gains and losses with an exact McNemar p, and whether the Shape count was kept.
- **Model calls:**
  - It calls through `gemini_api.py` (`call()`, `spent()`, shared ledger) when it can import it: from `$X3D_ONTO_EVAL_PLAN`, else `~/x3d.onto/xdomain/eval-plan`, else `/home/claude/x3d.onto/xdomain/eval-plan` (branch `loom/gemini-api`).
  - Otherwise it calls the Gemini REST API with `GEMINI_API_KEY`, else `GOOGLE_API_KEY`. `playground/config.py` and the eval-plan `runner.py` use Vertex with a service identity, so there was no key convention to copy.
- **Safety:** real calls need `--go`, and `--max-calls` caps them.
- **Plumbing check:** `--dry-run` runs a stub that applies the first exact edit. Its numbers say nothing about a model.

```
uv run python evaluation/exact_fix_eval.py                       # writes the corpus (deterministic: corpus sha256 7f5611e8...6e56 on two runs)
uv run python evaluation/hint_loop_gemini.py --dry-run           # free plumbing check
uv run python evaluation/hint_loop_gemini.py --go --limit 60 --max-calls 300
```

## Files

- `src/validation/hints.py`: the hint layer, `apply_fix`, `gate`, `repair_with_hints`.
- `src/validation/validate.py`: `validate_xml_with_hints` (`validate_xml` unchanged).
- `src/tools/validate_tool.py`: the new `exact_fix_hints` tool.
- `src/validation/schemas/verified-containment.json` and `evaluation/build_verified_containment.py`.
- `evaluation/exact_fix_eval.py` and `evaluation/exact_fix/results.json`. `corpus.jsonl` is regenerated by the script and is not committed.
- `evaluation/hint_loop_gemini.py`.
- `tests/test_hints.py`.
