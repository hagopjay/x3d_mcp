# X3D to Blender to USD: do the X3D checks predict the USD rulebooks? (Q12, first run)

Measured 26 Nov 2026. Receipt: `docs/usd-q12-blender-join-20261126.json`. Re-run: the three scripts in `tests/usd_q12/`.

## Instruments (every number below comes from these)
| What | Version |
|---|---|
| Blender (`bpy` from PyPI, `wm.usd_export`) | 5.3.0 |
| x3d-blender-io add-on (`import_scene.x3d`) | 2.7.6, `3e3c1e8` |
| OpenUSD `UsdValidation`, 30 validators | usd-core 26.11 (Python 3.12) |
| Omniverse Asset Validator, 40 rules in the pinned list | 1.18.0 (Python 3.12.3; it needs 3.12 or older) |
| SimReady Foundation | `c3bd1d8`: 270 requirement pages, **not executable**, so not run |
| X3D side | x3d_mcp `9e21c4c` (`tests/usd_parity.py` MAP and fixtures) |

Corpus: the 84 parity scenes (a failing and a passing scene for each of 42 fixtures). Real X3D files are not in this first run.

## Result (Confirmed: executed; rulebook pins above)
- **84 of 84 scenes imported and exported**; 0 rule crashes ("not run" 0).
- **70 rules executed** (30 usd-core + 40 Omniverse); **27 of them** are ones the MAP ties to an X3D check.
- **70 of 84 scenes agree on every mapped rule.** Of 1,776 scene-by-rule pairs: 1,748 both quiet, 8 both fire, 17 X3D only, 1 export-made, 2 candidates.
- All **30 usd-core validators passed on all 84 exports**: they never fired, so this run says nothing for or against them.

| Class | Pairs | Meaning |
|---|---|---|
| agree, both quiet | 1,748 | |
| agree, both fire | 8 | X3D predicted the USD failure after export |
| X3D only | 17 | X3D reports the fault, the USD rule passes the export |
| export-made | 1 | X3D quiet and the scene clean, the USD rule fails |
| X3D missed? | 2 | X3D quiet on that rule, USD fails, the scene has an X3D finding for another reason |

## Reading the 17 "X3D only" (Likely: the cause is inferred from the scene, not traced inside Blender)
Blender's X3D importer is tolerant, so the fault is gone before the USD exporter runs. USD never sees it. Examples: `type/bad` (a non-number radius) and `unknown-node/bad` and `nested-geometry/bad` (XSD faults), `pbr-range/bad`, `normal-unit/bad`, `body-shared/bad`, `joint-body/bad`, `inertia/bad`, `use-undefined/bad`, `index-oob/bad`. The practical reading is the STRATEGY claim "SimReady before export": for these 17 pairs the X3D validator is the only place the fault shows, because the exporter launders it.

## The 3 to look at
- `oav:ManifoldChecker` and `oav:WeldChecker` on `index-oob/bad` (an out-of-range index): Blender's repair leaves a mesh the USD rules find non-manifold and weldable. Suspected export-made. Not an x3d_mcp defect unless a person decides the X3D check should also say it.
- `oav:ManifoldChecker` on `unused-point/bad`: export-made, Suspected (an unused point becomes loose geometry).

## Export-made by construction
`oav:IndexedPrimvarChecker` fails on **37 of 84** exports, with no X3D check behind it: Blender writes primvars unindexed. File against the add-on or Blender, or add to `EXPECTED_LOSS`.

## Not shown
- Real files (1,548 are surveyed on the X3D side only).
- The 39 stateable rules (66 minus the 27 executed) with no executable USD rule here (SimReady requirement pages).
- `upAxis`/`metersPerUnit` rules: no usd-core rule fired, so FM 371 (Z up) is not tested by this run.
