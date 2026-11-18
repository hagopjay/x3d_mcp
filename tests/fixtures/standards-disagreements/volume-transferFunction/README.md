# Standards disagreement: OpacityMapVolumeStyle.transferFunction (X3D 4.0)

**Status: upstream report drafted, HELD.** Nothing has been sent to Web3D or any outside repository; it goes only
when the owner types "send". Found by the external accountability review of 18 Nov 2026 (defect 8) and re-checked
here.

| Source | Says | Verdict on `<PixelTexture containerField="transferFunction"/>` inside `OpacityMapVolumeStyle` |
|---|---|---|
| ISO/IEC 19775-1:2023, 41.4.7 | `SFNode [in,out] transferFunction NULL [X3DTexture2DNode,X3DTexture3DNode]` | conforming |
| X3DUOM 4.0 (bundled) | field `transferFunction`, types `X3DTexture2DNode\|X3DTexture3DNode` | conforming (0 semantic errors) |
| x3d-4.0.xsd, bundled (sha256 0509fa5b...) | `transferFunction` missing from both texture containerField enumerations | **invalid** |
| x3d-4.0.xsd, live web3d.org, fetched 18 Nov 2026 (1,336,330 bytes, sha256 d01ac41b...) | same omission | **invalid** |

The inverse also holds: a texture with no `containerField` (default `texture`, not a field of this node) passes the
XSD and is caught only by the semantic layer. So a schema snapshot is an incomplete proxy for conformance in both
directions, and x3d_mcp keeps the two verdicts apart.

Files: four conforming forms (2D and 3D, pixel and image), one non-conforming control, `expected.json` (the
verdicts, the clause, the hashes), and `UPSTREAM-ISSUE-DRAFT.md`. `tests/test_standards_disagreements.py` pins the
stock-XSD verdict and the semantic verdict separately, so the test turns red the day the XSD is corrected and the
pin must be updated deliberately.
