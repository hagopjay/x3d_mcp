# Scene brief: offline hardening pass

16 November 2026. No Jev account, model endpoint or network API was used.

## Repairs

| Defect | Result |
| --- | --- |
| Finite inputs could overflow during transform composition or corner projection and publish Infinity/NaN. | Overflowing subtrees/instances are reported as unbounded. An unrepresentable aggregate extent omits aggregate coordinates. Strict JSON serialization refuses nonfinite values. |
| `CADPart` was traversed as a plain group. | Its translation, centre, rotation, scale orientation and scale now use the transform path. |
| Squaring an extreme finite rotation axis overflowed during normalization. | Axis normalization scales components first. A zero axis with a nonzero angle is refused as an invalid value. |
| Fractional Switch choices were silently truncated to another child. | Invalid fractional choices are reported instead of selecting a different branch. |
| Nearest decimal rounding could shrink a supposedly conservative box. | Minima and maxima round outward to 0.001 scene units; displayed size and centre follow those endpoints. |
| `input.sha256` named a shortened, 16-hex digest. | `input.sha256_full` carries all 64 hex digits; the old 16-hex `sha256` is kept, as a display prefix, for compatibility. |

## Checked

```sh
python3 -m pytest tests/test_scene_brief_standalone.py tests/test_scene_brief.py -q
```

**16 standalone regressions pass; 1 host-dependent test module is skipped.** The skipped module contains the supplied original 50 integration tests and requires a complete compatible x3d_mcp checkout. The archive is an overlay: `tools.granular`, transfer machinery, validators and the production X3DUOM loader are absent. The core and tool modules compile successfully.

The standalone tests use a deliberately small vocabulary fixture and lxml. They verify the new numeric, budgeting and identity behavior. They do not certify X3DUOM conformance, schema validity, semantic validity, MCP transport or runtime rendering. The supplied benchmark table remains historical; it was not rerun.

## Loom boundary

A brief is an observation/slice, not acceptance. It does not mint a Loom witness. A judge can use relevant facets to prioritize investigation, while deterministic checks and actual renderer observations retain their separate authority. Do not include validator outcomes in a model input when those same outcomes supply its evaluation labels.

An authored static box is partial whenever unsupported or unbounded subtrees exist. It follows the selected Switch branch and first LOD level, without animation, scripts or remote resources. Census/material/light counts describe the document, including definitions, rather than a renderer traversal. A first Viewpoint position is an authored local field, not a measured active world camera. These distinctions prevent attractive compact state from becoming stronger evidence than the code actually collected.
