<!-- HELD: draft only. Not sent. Send only on the owner's typed "send". -->
# X3D 4.0 XSD: `transferFunction` missing from texture containerField enumerations

**Clause.** ISO/IEC 19775-1:2023 Part 1, 41.4.7 OpacityMapVolumeStyle:
`SFNode [in,out] transferFunction NULL [X3DTexture2DNode,X3DTexture3DNode]`. X3DUOM 4.0 agrees.

**Schema.** https://www.web3d.org/specifications/x3d-4.0.xsd (retrieved 18 Nov 2026, 1,336,330 bytes,
sha256 prefix d01ac41b4ac218ac): `containerFieldChoicesX3DTexture2DNode` and
`containerFieldChoicesX3DTexture3DNode` do not list `transferFunction`.

**Minimal example (conforming per the clause, rejected by the XSD):**
```xml
<X3D profile="Full" version="4.0"><Scene><Viewpoint/>
 <VolumeData dimensions="1 1 1">
  <PixelTexture3D containerField="voxels" image="2 2 2 1 0 255 0 255 255 0 255 0"/>
  <OpacityMapVolumeStyle><PixelTexture containerField="transferFunction" image="2 1 4 0x00000000 0xFFFFFFFF"/></OpacityMapVolumeStyle>
 </VolumeData></Scene></X3D>
```
libxml2 2.14.6 (lxml 6.1.3): `Element 'PixelTexture', attribute 'containerField': [facet 'enumeration'] The value
'transferFunction' is not an element of the set {...}`. Same for ImageTexture, PixelTexture3D, ImageTexture3D.

**Expected.** Valid. **Proposed fix.** Add `transferFunction` (parent OpacityMapVolumeStyle) to both enumerations;
the bundled x3d-4.1.xsd draft has the same omission.

Four conforming fixtures and one control: x3d_mcp `tests/fixtures/standards-disagreements/volume-transferFunction/`.
