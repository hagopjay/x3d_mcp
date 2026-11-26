"""glTF-Validator parity: every issue code Khronos glTF-Validator can raise (170, from its ISSUES.md),
mapped to what the X3D checks here do about the same mistake, with one failing and one passing scene
for each X3D check that is claimed.

    python tests/gltf_parity.py            run every fixture, print the result table
    python tests/gltf_parity.py --md       write docs/GLTF-VALIDATOR-PARITY.md from the executed results
    python tests/gltf_parity.py --json     print, per glTF-Validator code, whether its scene pair behaved

Status words used in MAP:
    xsd        the X3D 4.0 schema rejects it
    semantic   a semantic check that existed before this work reports it
    new        a check added for this work reports it (src/validation/data_checks.py)
    parse      the file does not parse
    binary     the mistake lives in glTF's byte layout (buffers, views, strides, GLB chunks); the X3D
               XML encoding holds numbers as text, so it cannot be written
    design     X3D's design leaves no way to state it
    feature    glTF feature X3D 4.0 does not have, so there is nothing to check
    x3d-4.1    the node (Tangent) is not in the X3D 4.0 schema; the check is written and tested for when it is
    gap        X3D can state it and nothing here reports it
"""
from __future__ import annotations

import struct
import sys
import tempfile
import zlib
from pathlib import Path

import json
import os

ROOT = Path(__file__).resolve().parent.parent
# X3D_PARITY_SRC points the same scenes at another checkout's src/, to measure an older validator
sys.path.insert(0, os.environ.get("X3D_PARITY_SRC") or str(ROOT / "src"))


def doc(body: str, head: str = "", profile: str = "Full", version: str = "4.0") -> str:
    return (f'<?xml version="1.0" encoding="UTF-8"?>\n<X3D profile="{profile}" version="{version}">'
            f'<head>{head}<meta name="title" content="parity"/></head><Scene><Viewpoint position="0 0 8"/>'
            f'{body}</Scene></X3D>')


def png(w=4, h=4, depth=8, cut=False) -> bytes:
    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    raw = b"".join(b"\x00" + b"\x80" * (w * 3 * depth // 8) for _ in range(h))
    out = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, depth, 2, 0, 0, 0)) + chunk(b"IDAT", zlib.compress(raw))
    return out if cut else out + chunk(b"IEND", b"")


QUAD = "<Coordinate point='0 0 0 1 0 0 1 1 0 0 1 0'/>"
APP = "<Appearance><Material/></Appearance>"


def ifs(attrs="coordIndex='0 1 2 3 -1'", kids=QUAD, app=APP):
    return f"<Shape>{app}<IndexedFaceSet {attrs}>{kids}</IndexedFaceSet></Shape>"


def tex(url, app_attrs=""):
    return (f"<Shape><Appearance {app_attrs}><Material/><ImageTexture url='{url}'/></Appearance>"
            f"<IndexedFaceSet coordIndex='0 1 2 3 -1'>{QUAD}<TextureCoordinate point='0 0 1 0 1 1 0 1'/></IndexedFaceSet></Shape>")


def anim(key="0 1", key_value="0 0 0 1 0 0", extra=""):
    return (f"<Transform DEF='T'><Shape>{APP}<Box/></Shape></Transform><TimeSensor DEF='Clock' loop='true'/>"
            f"<PositionInterpolator DEF='Move' key='{key}' keyValue='{key_value}'/>"
            f"<ROUTE fromNode='Clock' fromField='fraction_changed' toNode='Move' toField='set_fraction'/>"
            f"<ROUTE fromNode='Move' fromField='value_changed' toNode='T' toField='set_translation'/>{extra}")


def human(root_attrs="skinCoordIndex='0 1' skinCoordWeight='1 1'", child_attrs="skinCoordIndex='2 3' skinCoordWeight='1 1'",
          h_attrs="", joints="<HAnimJoint USE='hanim_humanoid_root' containerField='joints'/><HAnimJoint USE='hanim_sacroiliac' containerField='joints'/>",
          coord="<Coordinate DEF='SkinCoord' containerField='skinCoord' point='0 0 0 1 0 0 1 1 0 0 1 0'/>", extra="", child_kids=""):
    return (f"<HAnimHumanoid DEF='hanim_H' name='H' version='2.0' {h_attrs}>"
            f"<HAnimJoint DEF='hanim_humanoid_root' name='humanoid_root' containerField='skeleton' {root_attrs}>"
            f"<HAnimJoint DEF='hanim_sacroiliac' name='sacroiliac' {child_attrs}>{child_kids}</HAnimJoint></HAnimJoint>"
            f"{joints}{coord}"
            f"<Shape containerField='skin'>{APP}<IndexedFaceSet coordIndex='0 1 2 3 -1'><Coordinate USE='SkinCoord'/></IndexedFaceSet></Shape>"
            f"</HAnimHumanoid>{extra}")


IMG = {"tex.png": png(), "notes.png": b"these are notes, not an image\n", "cut.png": png(cut=True),
       "odd.png": png(5, 3), "deep.png": png(depth=16), "really.jpg": png(), "pic.bmp": b"BM" + b"\x00" * 60,
       "half.jpg": b"\xff\xd8\xff\xe0" + b"\x00" * 40}

# name: (by, check id or None, failing scene, passing scene)
F = {
    # ---- the schema
    "type": ("xsd", None, doc(f"<Shape>{APP}<Sphere radius='big'/></Shape>"), doc(f"<Shape>{APP}<Sphere radius='1'/></Shape>")),
    "array-type": ("xsd", None, doc(ifs("coordIndex='0 a 2 3 -1'")), doc(ifs())),
    "pattern": ("xsd", None, doc(f"<Shape DEF='1 bad'>{APP}<Box/></Shape>"), doc(f"<Shape DEF='Good'>{APP}<Box/></Shape>")),
    "required": ("xsd", None, doc(anim().replace(" toNode='T'", "")), doc(anim())),
    "unexpected-attr": ("xsd", None, doc(f"<Shape>{APP}<Sphere colour='1 0 0'/></Shape>"), doc(f"<Shape>{APP}<Sphere/></Shape>")),
    "unknown-child": ("xsd", None, doc(ifs(kids=QUAD + "<Weights/>")), doc(ifs())),
    "version": ("xsd", None, doc(ifs(), version="9.9"), doc(ifs())),
    "component-name": ("xsd", None, doc(ifs(), head="<component name='Bogus' level='1'/>"), doc(ifs())),
    "float": ("xsd", None, doc(ifs(kids="<Coordinate point='0 0 0 1 0 0 1 1 NaN 0 1 0'/>")), doc(ifs())),
    "enum": ("any", None, doc(tex('"tex.png"').replace("<ImageTexture url='\"tex.png\"'/>", "<ImageTexture url='\"tex.png\"'><TextureProperties magnificationFilter='BOGUS'/></ImageTexture>")),
             doc(tex('"tex.png"').replace("<ImageTexture url='\"tex.png\"'/>", "<ImageTexture url='\"tex.png\"'><TextureProperties magnificationFilter='NICEST'/></ImageTexture>"))),
    "fov": ("any", None, doc("<Viewpoint fieldOfView='3.5'/>" + ifs()), doc("<Viewpoint fieldOfView='0.8'/>" + ifs())),
    "range": ("any", None, doc(f"<Shape>{APP}<Sphere radius='-1'/></Shape>"), doc(f"<Shape>{APP}<Sphere radius='1'/></Shape>")),
    "not-xml": ("parse", None, doc(ifs()).replace("</Scene>", ""), doc(ifs())),
    # ---- semantic checks that existed before
    "tuple-count": ("any", "tuple-count", doc(f"<Transform translation='1 2'><Shape>{APP}<Box/></Shape></Transform>"),
                    doc(f"<Transform translation='1 2 3'><Shape>{APP}<Box/></Shape></Transform>")),
    "duplicate-def": ("any", "duplicate-def", doc(f"<Shape DEF='A'>{APP}<Box/></Shape><Shape DEF='A'>{APP}<Box/></Shape>"),
                      doc(f"<Shape DEF='A'>{APP}<Box/></Shape><Shape DEF='B'>{APP}<Box/></Shape>")),
    "empty-group": ("sem", "empty-group", doc("<Group/>" + ifs()), doc(f"<Group>{ifs()}</Group>")),
    "negative-index": ("any", "index-out-of-range", doc(ifs("coordIndex='0 1 -2 3 -1'")), doc(ifs())),
    "index-oob": ("sem", "index-out-of-range", doc(ifs("coordIndex='0 1 2 9 -1'")), doc(ifs())),
    "sfnode-multiple": ("sem", "sfnode-multiple", doc("<Shape><Appearance><Material/><Material/></Appearance><Box/></Shape>"), doc(f"<Shape>{APP}<Box/></Shape>")),
    "multiple-of": ("sem", "array-count", doc(f"<Shape>{APP}<IndexedTriangleSet index='0 1 2 3'>{QUAD}</IndexedTriangleSet></Shape>"),
                    doc(f"<Shape>{APP}<IndexedTriangleSet index='0 1 2 0 2 3'>{QUAD}</IndexedTriangleSet></Shape>")),
    "profile": ("sem", "profile-component", doc(ifs(), profile="Interchange").replace("<Scene>", "<Scene><NurbsCurve/>"), doc(ifs(), profile="Interchange")),
    "route-field": ("sem", "route-invalid-to-field", doc(anim().replace("toField='set_translation'", "toField='set_weights'")), doc(anim())),
    "route-type": ("sem", "route-type-mismatch", doc(anim().replace("toField='set_translation'", "toField='set_rotation'")), doc(anim())),
    "route-missing": ("sem", "route-missing-to-node", doc(anim().replace("toNode='T'", "toNode='Nobody'")), doc(anim())),
    "use-undefined": ("sem", "use-undefined-def", doc(f"<Shape>{APP}<Box/></Shape><Shape USE='Nobody'/>"), doc(f"<Shape DEF='S'>{APP}<Box/></Shape><Shape USE='S'/>")),
    "unused-def": ("sem-info", "unused-def", doc(f"<Shape>{APP}<Box/></Shape><Group DEF='Spare'><Shape>{APP}<Box/></Shape></Group>"), doc(f"<Shape>{APP}<Box/></Shape>")),
    "key-length": ("sem", "interpolator-key-length", doc(anim("0 0.5 1", "0 0 0 1 0 0")), doc(anim())),
    "key-order": ("sem", "interpolator-key-order", doc(anim("0 1 0.5", "0 0 0 1 0 0 2 0 0")), doc(anim("0 0.5 1", "0 0 0 1 0 0 2 0 0"))),
    "colour-range": ("any", "colour-out-of-range", doc(ifs(kids=QUAD + "<Color color='1 0 0 0 2 0 0 0 1 1 1 1'/>")), doc(ifs(kids=QUAD + "<Color color='1 0 0 0 1 0 0 0 1 1 1 1'/>"))),
    # ---- checks added for this work
    "use-type": ("sem", "use-type-mismatch", doc(f"<Shape DEF='S'><Appearance DEF='A'><Material/></Appearance><Box/></Shape><Shape><Material USE='A'/><Box/></Shape>"),
                 doc(f"<Shape DEF='S'><Appearance DEF='A'><Material/></Appearance><Box/></Shape><Shape><Appearance USE='A'/><Box/></Shape>")),
    "use-cycle": ("sem", "use-cycle", doc(f"<Group DEF='G'><Shape>{APP}<Box/></Shape><Group USE='G'/></Group>"), doc(f"<Group DEF='G'><Shape>{APP}<Box/></Shape></Group><Group USE='G'/>")),
    "route-duplicate": ("sem", "route-duplicate", doc(anim(extra="<ROUTE fromNode='Move' fromField='value_changed' toNode='T' toField='set_translation'/>")), doc(anim())),
    "route-fan-in": ("sem", "route-fan-in", doc(anim(extra="<PositionInterpolator DEF='Move2' key='0 1' keyValue='0 0 0 0 1 0'/><ROUTE fromNode='Clock' fromField='fraction_changed' toNode='Move2' toField='set_fraction'/><ROUTE fromNode='Move2' fromField='value_changed' toNode='T' toField='set_translation'/>")), doc(anim())),
    "attribute-count": ("sem", "attribute-count-mismatch", doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1'/>")), doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/>"))),
    "face-size": ("sem", "face-too-few-vertices", doc(ifs("coordIndex='0 1 -1 0 1 2 3 -1'")), doc(ifs())),
    "degenerate": ("sem", "degenerate-face", doc(ifs("coordIndex='0 1 1 -1 0 1 2 3 -1'")), doc(ifs())),
    "no-coordinate": ("sem", "geometry-no-coordinate", doc(ifs(kids="")), doc(ifs())),
    "normal-unit": ("sem", "normal-not-unit", doc(ifs(kids=QUAD + "<Normal vector='0 0 3 0 0 1 0 0 1 0 0 1'/>")), doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/>"))),
    "rotation-axis": ("sem", "rotation-axis-not-unit", doc(f"<Transform rotation='0 2 0 1.57'><Shape>{APP}<Box/></Shape></Transform>"), doc(f"<Transform rotation='0 1 0 1.57'><Shape>{APP}<Box/></Shape></Transform>")),
    "rotation-key": ("sem", "rotation-axis-not-unit", doc("<OrientationInterpolator DEF='O' key='0 1' keyValue='0 3 0 0 0 1 0 1'/>" + ifs()), doc("<OrientationInterpolator DEF='O' key='0 1' keyValue='0 1 0 0 0 1 0 1'/>" + ifs())),
    "scale-zero": ("sem", "scale-zero", doc(f"<Transform scale='1 0 1'><Shape>{APP}<Box/></Shape></Transform>"), doc(f"<Transform scale='1 2 1'><Shape>{APP}<Box/></Shape></Transform>")),
    "clip": ("sem", "clip-distance", doc("<Viewpoint nearDistance='10' farDistance='1'/>" + ifs()), doc("<Viewpoint nearDistance='1' farDistance='10'/>" + ifs())),
    "ortho": ("sem", "ortho-extent", doc("<OrthoViewpoint fieldOfView='-1 -1 -1 1'/>" + ifs()), doc("<OrthoViewpoint fieldOfView='-1 -1 1 1'/>" + ifs())),
    "spot": ("sem", "spot-beam-wider-than-cutoff", doc("<SpotLight beamWidth='1.2' cutOffAngle='0.6'/>" + ifs()), doc("<SpotLight beamWidth='0.4' cutOffAngle='0.6'/>" + ifs())),
    "alpha-cutoff": ("sem", "alpha-cutoff-unused", doc(tex('"tex.png"', "alphaMode='BLEND' alphaCutoff='0.3'")), doc(tex('"tex.png"', "alphaMode='MASK' alphaCutoff='0.3'"))),
    "texture-mapping": ("sem", "texture-mapping-unresolved",
                        doc("<Shape><Appearance><PhysicalMaterial baseTextureMapping='uv2'><ImageTexture containerField='baseTexture' url='\"tex.png\"'/></PhysicalMaterial></Appearance>" + f"<IndexedFaceSet coordIndex='0 1 2 3 -1'>{QUAD}<TextureCoordinate mapping='uv1' point='0 0 1 0 1 1 0 1'/></IndexedFaceSet></Shape>"),
                        doc("<Shape><Appearance><PhysicalMaterial baseTextureMapping='uv1'><ImageTexture containerField='baseTexture' url='\"tex.png\"'/></PhysicalMaterial></Appearance>" + f"<IndexedFaceSet coordIndex='0 1 2 3 -1'>{QUAD}<TextureCoordinate mapping='uv1' point='0 0 1 0 1 1 0 1'/></IndexedFaceSet></Shape>")),
    "pixel-texture": ("sem", "pixel-texture-count", doc("<Shape><Appearance><Material/><PixelTexture image='2 2 1 0xFF 0x00 0xFF'/></Appearance><Box/></Shape>"), doc("<Shape><Appearance><Material/><PixelTexture image='2 2 1 0xFF 0x00 0xFF 0x00'/></Appearance><Box/></Shape>")),
    "mfstring": ("sem", "mfstring-unquoted", doc(tex("tex.png")), doc(tex('"tex.png"'))),
    "morph-count": ("sem", "interpolator-target-count",
                    doc(ifs(kids="<Coordinate DEF='C' point='0 0 0 1 0 0 1 1 0 0 1 0'/>") + "<CoordinateInterpolator DEF='Morph' key='0 1' keyValue='0 0 0 1 0 0 1 1 0  0 0 1 1 0 1 1 1 1'/><ROUTE fromNode='Morph' fromField='value_changed' toNode='C' toField='set_point'/>"),
                    doc(ifs(kids="<Coordinate DEF='C' point='0 0 0 1 0 0 1 1 0 0 1 0'/>") + "<CoordinateInterpolator DEF='Morph' key='0 1' keyValue='0 0 0 1 0 0 1 1 0 0 1 0  0 0 1 1 0 1 1 1 1 0 1 1'/><ROUTE fromNode='Morph' fromField='value_changed' toNode='C' toField='set_point'/>")),
    "bbox": ("sem", "bbox-too-small", doc(f"<Group bboxCenter='0 0 0' bboxSize='1 1 1'><Transform translation='5 0 0'>{ifs()}</Transform></Group>"),
             doc(f"<Group bboxCenter='5.5 0.5 0' bboxSize='1 1 1'><Transform translation='5 0 0'>{ifs()}</Transform></Group>")),
    "index-unused": ("sem-info", "index-without-node", doc(ifs("coordIndex='0 1 2 3 -1' colorIndex='0 1 2 3 -1'")), doc(ifs())),
    "key-unreachable": ("sem", "interpolator-key-unreachable", doc(anim("-1 2", "0 0 0 1 0 0")), doc(anim())),
    "tangent-normal": ("sem", "tangent-without-normal", doc(ifs(kids=QUAD + "<Tangent vector='1 0 0 1 1 0 0 1 1 0 0 1 1 0 0 1'/>")),
                       doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/><Tangent vector='1 0 0 1 1 0 0 1 1 0 0 1 1 0 0 1'/>"))),
    "tangent-sign": ("sem", "tangent-sign", doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/><Tangent vector='1 0 0 0.5 1 0 0 1 1 0 0 1 1 0 0 1'/>")),
                     doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/><Tangent vector='1 0 0 -1 1 0 0 1 1 0 0 1 1 0 0 1'/>"))),
    "tangent-unused": ("sem-info", "tangent-unused", doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/><Tangent vector='1 0 0 1 1 0 0 1 1 0 0 1 1 0 0 1'/>")), doc(ifs())),
    "tangent-generated": ("sem-info", "normal-texture-generated-tangents",
                          doc("<Shape><Appearance><PhysicalMaterial><ImageTexture containerField='normalTexture' url='\"tex.png\"'/></PhysicalMaterial></Appearance>" + f"<IndexedFaceSet coordIndex='0 1 2 3 -1'>{QUAD}</IndexedFaceSet></Shape>"),
                          doc("<Shape><Appearance><PhysicalMaterial><ImageTexture containerField='normalTexture' url='\"tex.png\"'/></PhysicalMaterial></Appearance>" + f"<IndexedFaceSet coordIndex='0 1 2 3 -1'>{QUAD}<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/></IndexedFaceSet></Shape>")),
    "component-unused": ("sem-info", "component-unused", doc(ifs(), profile="Interchange", head="<component name='NURBS' level='1'/>"), doc(ifs(), profile="Interchange")),
    "url-absolute": ("sem", "url-absolute-path", doc(tex('"C:/Users/me/tex.png"')), doc(tex('"tex.png"'))),
    # ---- skinned humanoids
    "skin-index": ("sem", "hanim-skin-index-out-of-range", doc(human(child_attrs="skinCoordIndex='2 9' skinCoordWeight='1 1'")), doc(human())),
    "skin-weight-count": ("sem", "hanim-skin-weight-count", doc(human(child_attrs="skinCoordIndex='2 3' skinCoordWeight='1'")), doc(human())),
    "skin-weight-negative": ("sem", "hanim-skin-weight-negative", doc(human(child_attrs="skinCoordIndex='2 3' skinCoordWeight='1 -1'")), doc(human())),
    "skin-weight-sum": ("sem", "hanim-skin-weight-sum", doc(human(child_attrs="skinCoordIndex='2 3' skinCoordWeight='1 0.4'")), doc(human())),
    "skin-index-duplicate": ("sem", "hanim-skin-index-duplicate", doc(human(child_attrs="skinCoordIndex='2 3 3' skinCoordWeight='1 0.5 0.5'")), doc(human())),
    "skin-weight-zero": ("sem", "hanim-skin-weight-zero", doc(human(root_attrs="skinCoordIndex='0 1 2' skinCoordWeight='1 1 0'")), doc(human())),
    "skin-unbound": ("sem", "hanim-skin-unbound", doc(human(root_attrs="", child_attrs="")), doc(human())),
    "skin-coord-missing": ("sem", "hanim-skin-coord-missing", doc(human(coord="").replace("<Coordinate USE='SkinCoord'/>", QUAD)), doc(human())),
    "joint-outside": ("sem", "hanim-joint-outside-skeleton", doc(human(joints="<HAnimJoint USE='hanim_humanoid_root' containerField='joints'/><HAnimJoint USE='hanim_l_hip' containerField='joints'/>", extra="<HAnimJoint DEF='hanim_l_hip' name='l_hip'/>")), doc(human())),
    "binding-count": ("sem", "hanim-binding-count", doc(human(h_attrs="jointBindingPositions='0 0 0'")), doc(human(h_attrs="jointBindingPositions='0 0 0 0 1 0'"))),
    "displacer": ("sem", "hanim-displacer-count", doc(human(child_kids="<HAnimDisplacer name='x' coordIndex='2 3' displacements='0 0 1'/>")), doc(human(child_kids="<HAnimDisplacer name='x' coordIndex='2 3' displacements='0 0 1 0 0 1'/>"))),
    # ---- files the scene names (run with the scene's folder)
    "file-missing": ("file", "url-unresolved", doc(tex('"gone.png"')), doc(tex('"tex.png"'))),
    "file-not-image": ("file", "image-unrecognized", doc(tex('"notes.png"')), doc(tex('"tex.png"'))),
    "file-extension": ("file", "image-extension-mismatch", doc(tex('"really.jpg"')), doc(tex('"tex.png"'))),
    "file-truncated": ("file", "image-truncated", doc(tex('"cut.png"')), doc(tex('"tex.png"'))),
    "file-truncated-jpeg": ("file", "image-truncated", doc(tex('"half.jpg"')), doc(tex('"tex.png"'))),
    "file-npot": ("file-info", "image-npot", doc(tex('"odd.png"')), doc(tex('"tex.png"'))),
    "file-colour": ("file-info", "image-colour-info", doc(tex('"deep.png"')), doc(tex('"tex.png"'))),
    "file-optional-format": ("file-info", "image-format-optional", doc(tex('"pic.bmp"')), doc(tex('"tex.png"'))),
    "inline-missing": ("file", "url-unresolved", doc("<Inline url='\"gone.x3d\"'/>" + ifs()), doc(ifs())),
}

B, D, X = "binary", "design", "feature"
MAP = {  # glTF-Validator code: (status, fixture or None, note)
    "IO_ERROR": ("new", "file-missing", "a named file cannot be read"),
    "ARRAY_LENGTH_NOT_IN_LIST": ("semantic", "tuple-count", ""), "ARRAY_TYPE_MISMATCH": ("xsd", "array-type", ""),
    "DUPLICATE_ELEMENTS": ("semantic", "duplicate-def", "also route-duplicate"), "EMPTY_ENTITY": ("semantic", "empty-group", ""),
    "INVALID_INDEX": ("semantic", "negative-index", "-1 ends a face; any other negative index is rejected"),
    "INVALID_JSON": ("parse", "not-xml", "XML well-formedness"), "INVALID_URI": ("new", "mfstring", "url must be a list of quoted strings"),
    "ONE_OF_MISMATCH": ("semantic", "sfnode-multiple", "one node per single-node field"), "PATTERN_MISMATCH": ("xsd", "pattern", ""),
    "TYPE_MISMATCH": ("xsd", "type", ""), "UNDEFINED_PROPERTY": ("xsd", "required", ""), "UNEXPECTED_PROPERTY": ("xsd", "unexpected-attr", "an error in X3D, a warning in glTF"),
    "UNSATISFIED_DEPENDENCY": ("semantic", "profile", "a node needs its component declared"), "VALUE_MULTIPLE_OF": ("semantic", "multiple-of", ""),
    "VALUE_NOT_IN_LIST": ("xsd", "enum", ""), "VALUE_NOT_IN_RANGE": ("xsd", "range", ""),
    "ACCESSOR_MATRIX_ALIGNMENT": (B, None, ""), "ACCESSOR_NORMALIZED_INVALID": (B, None, ""), "ACCESSOR_OFFSET_ALIGNMENT": (B, None, ""),
    "ACCESSOR_SPARSE_COUNT_OUT_OF_RANGE": (B, None, ""),
    "ANIMATION_CHANNEL_TARGET_NODE_SKIN": (D, None, "an HAnim skin lives inside its humanoid, and transforms above it do apply"),
    "ASSET_MIN_VERSION_GREATER_THAN_VERSION": (D, None, "X3D has one version attribute"),
    "BUFFER_DATA_URI_MIME_TYPE_INVALID": (B, None, ""), "BUFFER_VIEW_INVALID_BYTE_STRIDE": (B, None, ""), "BUFFER_VIEW_TOO_BIG_BYTE_STRIDE": (B, None, ""),
    "CAMERA_XMAG_YMAG_NEGATIVE": ("new", "ortho", ""), "CAMERA_XMAG_YMAG_ZERO": ("new", "ortho", ""), "CAMERA_YFOV_GEQUAL_PI": ("xsd", "fov", "an error in X3D, a warning in glTF"),
    "CAMERA_ZFAR_LEQUAL_ZNEAR": ("new", "clip", ""), "EXTRA_PROPERTY": ("new", "index-unused", ""),
    "INVALID_EXTENSION_NAME_FORMAT": ("xsd", "component-name", "component names are a closed list"), "INVALID_GL_VALUE": ("xsd", "enum", "sampler modes are named, not GL numbers"),
    "KHR_ANIMATION_POINTER_ANIMATION_CHANNEL_TARGET_NODE": ("semantic", "route-field", "a ROUTE can target any field natively; the field must exist"),
    "KHR_ANIMATION_POINTER_ANIMATION_CHANNEL_TARGET_PATH": ("semantic", "route-type", "and its type must match"),
    "KHR_LIGHTS_PUNCTUAL_LIGHT_SPOT_ANGLES": ("new", "spot", ""),
    "KHR_MATERIALS_ANISOTROPY_ANISOTROPY_TEXTURE_TEXCOORD": (X, None, "no anisotropy in X3D 4.0"), "KHR_MATERIALS_CLEARCOAT_CLEARCOAT_NORMAL_TEXTURE_TEXCOORD": (X, None, "no clearcoat"),
    "KHR_MATERIALS_DISPERSION_NO_VOLUME": (X, None, "no dispersion"), "KHR_MATERIALS_EMISSIVE_STRENGTH_ZERO_FACTOR": (X, None, "no emissive strength"),
    "KHR_MATERIALS_IRIDESCENCE_THICKNESS_RANGE_WITHOUT_TEXTURE": (X, None, "no iridescence"), "KHR_MATERIALS_IRIDESCENCE_THICKNESS_TEXTURE_UNUSED": (X, None, "no iridescence"),
    "KHR_MATERIALS_VOLUME_DOUBLE_SIDED": (X, None, "no volume material"), "KHR_MATERIALS_VOLUME_NO_TRANSMISSION": (X, None, "no volume material"),
    "MATERIAL_ALPHA_CUTOFF_INVALID_MODE": ("new", "alpha-cutoff", ""),
    "MESH_INVALID_WEIGHTS_COUNT": (D, None, "each HAnimDisplacer carries its own single weight; there are no parallel arrays"),
    "MESH_PRIMITIVES_UNEQUAL_TARGETS_COUNT": (D, None, "same"), "MESH_PRIMITIVE_INDEXED_SEMANTIC_CONTINUITY": (D, None, "texture coordinates are named by mapping, not numbered"),
    "MESH_PRIMITIVE_INVALID_ATTRIBUTE": ("xsd", "unknown-child", ""), "MESH_PRIMITIVE_JOINTS_WEIGHTS_MISMATCH": ("new", "skin-weight-count", ""),
    "MESH_PRIMITIVE_NO_POSITION": ("new", "no-coordinate", ""), "MESH_PRIMITIVE_TANGENT_WITHOUT_NORMAL": ("x3d-4.1", "tangent-normal", "Tangent node"),
    "MULTIPLE_EXTENSIONS": (X, None, ""), "NODE_EMPTY": ("semantic", "empty-group", "a warning in X3D, information in glTF"),
    "NODE_MATRIX_DEFAULT": (D, None, "Transform has no matrix field"), "NODE_MATRIX_NON_TRS": (D, None, "same"), "NODE_MATRIX_TRS": (D, None, "same"),
    "NODE_SKINNED_MESH_LOCAL_TRANSFORMS": (D, None, "HAnim skin is inside the humanoid"), "NODE_SKINNED_MESH_NON_ROOT": (D, None, "same"), "NODE_SKINNED_MESH_PARENT_TRANSFORMS": (D, None, "same"),
    "NODE_SKIN_NO_SCENE": ("new", "joint-outside", ""), "NON_OBJECT_EXTRAS": (D, None, "metadata nodes are typed"), "NON_RELATIVE_URI": ("new", "url-absolute", ""),
    "NON_REQUIRED_EXTENSION": (D, None, "components are always required"), "ROTATION_NON_UNIT": ("new", "rotation-axis", ""),
    "SKIN_NO_COMMON_ROOT": ("new", "joint-outside", ""), "SKIN_SKELETON_INVALID": ("new", "joint-outside", ""),
    "UNKNOWN_ASSET_MAJOR_VERSION": ("xsd", "version", ""), "UNKNOWN_ASSET_MINOR_VERSION": ("xsd", "version", ""), "UNUSED_EXTENSION_REQUIRED": ("new", "component-unused", "information here, an error in glTF"),
    "ACCESSOR_SMALL_BYTESTRIDE": (B, None, ""), "ACCESSOR_TOO_LONG": (B, None, ""), "ACCESSOR_TOTAL_OFFSET_ALIGNMENT": (B, None, ""), "ACCESSOR_USAGE_OVERRIDE": (B, None, ""),
    "ANIMATION_CHANNEL_TARGET_NODE_MATRIX": (D, None, "no matrix field"), "ANIMATION_CHANNEL_TARGET_NODE_WEIGHTS_NO_MORPHS": ("semantic", "route-field", "routing to a field the node does not have"),
    "ANIMATION_DUPLICATE_TARGETS": ("new", "route-fan-in", "also route-duplicate"), "ANIMATION_SAMPLER_ACCESSOR_WITH_BYTESTRIDE": (B, None, ""),
    "ANIMATION_SAMPLER_INPUT_ACCESSOR_INVALID_FORMAT": ("xsd", "array-type", "key is a float list"), "ANIMATION_SAMPLER_INPUT_ACCESSOR_TOO_FEW_ELEMENTS": ("semantic", "key-length", ""),
    "ANIMATION_SAMPLER_INPUT_ACCESSOR_WITHOUT_BOUNDS": (B, None, "keys are read, not declared"), "ANIMATION_SAMPLER_OUTPUT_ACCESSOR_INVALID_COUNT": ("semantic", "key-length", ""),
    "ANIMATION_SAMPLER_OUTPUT_ACCESSOR_INVALID_FORMAT": ("semantic", "route-type", ""), "BUFFER_MISSING_GLB_DATA": (B, None, ""), "BUFFER_VIEW_TARGET_MISSING": (B, None, ""),
    "BUFFER_VIEW_TARGET_OVERRIDE": (B, None, ""), "BUFFER_VIEW_TOO_LONG": (B, None, ""), "IMAGE_BUFFER_VIEW_WITH_BYTESTRIDE": (B, None, ""),
    "INCOMPLETE_EXTENSION_SUPPORT": (X, None, "about the validator, not the file"), "INVALID_IBM_ACCESSOR_COUNT": ("new", "binding-count", "HAnim binding arrays pair with joints"),
    "KHR_MATERIALS_VARIANTS_NON_UNIQUE_VARIANT": (X, None, "no material variants"), "MESH_PRIMITIVE_ACCESSOR_UNALIGNED": (B, None, ""), "MESH_PRIMITIVE_ACCESSOR_WITHOUT_BYTESTRIDE": (B, None, ""),
    "MESH_PRIMITIVE_ATTRIBUTES_ACCESSOR_INVALID_FORMAT": ("semantic", "tuple-count", ""), "MESH_PRIMITIVE_ATTRIBUTES_ACCESSOR_UNSIGNED_INT": (B, None, ""),
    "MESH_PRIMITIVE_GENERATED_TANGENT_SPACE": ("new", "tangent-generated", ""), "MESH_PRIMITIVE_INCOMPATIBLE_MODE": ("new", "face-size", "an error in X3D, a warning in glTF"),
    "MESH_PRIMITIVE_INDICES_ACCESSOR_INVALID_FORMAT": ("xsd", "array-type", ""), "MESH_PRIMITIVE_INDICES_ACCESSOR_WITH_BYTESTRIDE": (B, None, ""),
    "MESH_PRIMITIVE_MORPH_TARGET_INVALID_ATTRIBUTE_COUNT": ("new", "morph-count", "also hanim-displacer-count"), "MESH_PRIMITIVE_MORPH_TARGET_NO_BASE_ACCESSOR": ("semantic", "route-missing", ""),
    "MESH_PRIMITIVE_NO_TANGENT_SPACE": ("new", "tangent-generated", "X3D browsers generate one, so information not error"), "MESH_PRIMITIVE_POSITION_ACCESSOR_WITHOUT_BOUNDS": (D, None, "a bounding box is optional"),
    "MESH_PRIMITIVE_TOO_FEW_TEXCOORDS": ("new", "texture-mapping", ""), "MESH_PRIMITIVE_UNEQUAL_ACCESSOR_COUNT": ("new", "attribute-count", ""),
    "NODE_LOOP": ("new", "use-cycle", ""), "NODE_PARENT_OVERRIDE": (D, None, "a node may have several parents through USE"), "NODE_SKINNED_MESH_WITHOUT_SKIN": ("new", "skin-coord-missing", ""),
    "NODE_SKIN_WITH_NON_SKINNED_MESH": ("new", "skin-unbound", ""), "NODE_WEIGHTS_INVALID": (D, None, "see MESH_INVALID_WEIGHTS_COUNT"), "SCENE_NON_ROOT_NODE": (D, None, "the scene is written as a tree"),
    "SKIN_IBM_ACCESSOR_WITH_BYTESTRIDE": (B, None, ""), "SKIN_IBM_INVALID_FORMAT": (B, None, ""), "TEXTURE_INVALID_IMAGE_MIME_TYPE": ("new", "file-not-image", ""),
    "UNDECLARED_EXTENSION": ("semantic", "profile", ""), "UNEXPECTED_EXTENSION_OBJECT": ("xsd", "unknown-child", ""), "UNRESOLVED_REFERENCE": ("semantic", "use-undefined", "also use-type-mismatch, route-missing-to-node"),
    "UNSUPPORTED_EXTENSION": (X, None, "about the validator"), "UNUSED_MESH_TANGENT": ("x3d-4.1", "tangent-unused", "Tangent node"), "UNUSED_MESH_WEIGHTS": (D, None, "see MESH_INVALID_WEIGHTS_COUNT"),
    "UNUSED_OBJECT": ("semantic", "unused-def", ""),
    "ACCESSOR_ANIMATION_INPUT_NEGATIVE": ("new", "key-unreachable", ""), "ACCESSOR_ANIMATION_INPUT_NON_INCREASING": ("semantic", "key-order", ""),
    "ACCESSOR_ANIMATION_SAMPLER_OUTPUT_NON_NORMALIZED_QUATERNION": ("new", "rotation-key", ""), "ACCESSOR_ELEMENT_OUT_OF_MAX_BOUND": ("new", "bbox", "through static transforms too"),
    "ACCESSOR_ELEMENT_OUT_OF_MIN_BOUND": ("new", "bbox", ""), "ACCESSOR_INDEX_OOB": ("semantic", "index-oob", ""),
    "ACCESSOR_INDEX_PRIMITIVE_RESTART": (D, None, "-1 is the stated face separator"), "ACCESSOR_INDEX_TRIANGLE_DEGENERATE": ("new", "degenerate", "a warning here, information in glTF"),
    "ACCESSOR_INVALID_FLOAT": ("xsd", "float", ""), "ACCESSOR_INVALID_IBM": (D, None, "HAnim binds by joint centre, not by matrix"), "ACCESSOR_INVALID_SIGN": ("x3d-4.1", "tangent-sign", "Tangent node"),
    "ACCESSOR_JOINTS_INDEX_DUPLICATE": ("new", "skin-index-duplicate", ""), "ACCESSOR_JOINTS_INDEX_OOB": ("new", "skin-index", ""), "ACCESSOR_JOINTS_USED_ZERO_WEIGHT": ("new", "skin-weight-zero", ""),
    "ACCESSOR_MAX_MISMATCH": ("new", "bbox", "X3D asks the box to enclose, not to be exact, so a box that is too big passes"), "ACCESSOR_MIN_MISMATCH": ("new", "bbox", "same"),
    "ACCESSOR_NON_CLAMPED": ("semantic", "colour-range", ""), "ACCESSOR_SPARSE_INDEX_OOB": (B, None, ""), "ACCESSOR_SPARSE_INDICES_NON_INCREASING": (B, None, ""),
    "ACCESSOR_VECTOR3_NON_UNIT": ("new", "normal-unit", ""), "ACCESSOR_WEIGHTS_NEGATIVE": ("new", "skin-weight-negative", ""), "ACCESSOR_WEIGHTS_NON_NORMALIZED": ("new", "skin-weight-sum", ""),
    "BUFFER_BYTE_LENGTH_MISMATCH": (B, None, ""), "BUFFER_GLB_CHUNK_TOO_BIG": (B, None, ""), "DATA_URI_GLB": (B, None, ""),
    "IMAGE_DATA_INVALID": ("new", "file-truncated", "also pixel-texture-count"), "IMAGE_FEATURES_UNSUPPORTED": ("new", "file-colour", "16-bit PNG, colour profile, progressive JPEG"),
    "IMAGE_MIME_TYPE_INVALID": ("new", "file-extension", ""), "IMAGE_NON_ENABLED_MIME_TYPE": ("new", "file-optional-format", ""), "IMAGE_NPOT_DIMENSIONS": ("new", "file-npot", ""),
    "IMAGE_UNEXPECTED_EOS": ("new", "file-truncated-jpeg", ""), "IMAGE_UNRECOGNIZED_FORMAT": ("new", "file-not-image", "an error here, a warning in glTF"), "URI_GLB": (B, None, ""),
}
for _c in ("CHUNK_LENGTH_UNALIGNED", "CHUNK_TOO_BIG", "DUPLICATE_CHUNK", "EMPTY_BIN_CHUNK", "EMPTY_CHUNK", "EXTRA_DATA", "INVALID_MAGIC", "INVALID_VERSION",
           "LENGTH_MISMATCH", "LENGTH_TOO_SMALL", "UNEXPECTED_BIN_CHUNK", "UNEXPECTED_END_OF_CHUNK_DATA", "UNEXPECTED_END_OF_CHUNK_HEADER",
           "UNEXPECTED_END_OF_HEADER", "UNEXPECTED_FIRST_CHUNK", "UNKNOWN_CHUNK_TYPE"):
    MAP["GLB_" + _c] = (B, None, "GLB container")

# X3D checks with no glTF-Validator counterpart at all (the "or better" column)
BEYOND = {
    "scale-zero": "a zero scale component (glTF-Validator accepts scale 0 0 0)",
    "pixel-texture": "inline pixel data that does not match its declared size",
    "inline-missing": "a missing Inline scene file",
    "displacer": "HAnimDisplacer arrays that do not pair up",
}


def findings(text: str, folder=None):
    """(parse_ok, xsd error count, [(level, check)])."""
    from lxml import etree
    from validation.validate import validate_xml
    from validation import semantic
    try:
        etree.fromstring(text.encode())
    except etree.XMLSyntaxError:
        return False, 0, []
    xsd = len(validate_xml(text).get("errors") or [])
    with_files = getattr(semantic, "semantic_diagnostics_with_files", None)      # absent in older checkouts
    ds = with_files(text, folder) if folder and with_files else semantic.semantic_diagnostics(text)
    return True, xsd, [(d.level, d.check) for d in ds]


def run_fixture(name: str, folder: Path) -> dict:
    by, check, bad, good = F[name]
    use_folder = folder if by.startswith("file") else None
    levels = ("error", "warning", "info") if by.endswith("-info") else ("error", "warning")
    res = {}
    for label, text in (("bad", bad), ("good", good)):
        ok, xsd, ds = findings(text, use_folder)
        hit_sem = [c for lv, c in ds if lv in levels and (check is None or c == check)]
        if by == "parse":
            res[label] = not ok
        elif by == "xsd":
            res[label] = xsd > 0
        elif by == "any":
            res[label] = xsd > 0 or bool(hit_sem)
            res[label + "_by"] = "xsd" if xsd else ("semantic" if hit_sem else "")
        else:
            res[label] = bool(hit_sem)
        res[label + "_all"] = (xsd, sorted({c for lv, c in ds if lv in ("error", "warning")}))
    return res


def run_all() -> dict:
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        for n, b in IMG.items():
            (folder / n).write_bytes(b)
        return {name: run_fixture(name, folder) for name in F}


def status_of(code: str, results: dict) -> str:
    """The status in MAP, except that a scene the schema already rejects is counted as xsd."""
    status, fx, _ = MAP[code]
    if fx and F[fx][0] == "any" and status in ("semantic", "xsd"):
        return "xsd" if results[fx].get("bad_by") == "xsd" else "semantic"
    return status


def counts(results: dict) -> dict:
    order = ["xsd", "semantic", "new", "parse", "x3d-4.1", "binary", "design", "feature", "gap"]
    return {k: sum(1 for c in MAP if status_of(c, results) == k) for k in order}


def markdown(results: dict) -> str:
    count = counts(results)
    stateable = sum(count[k] for k in ("xsd", "semantic", "new", "parse", "gap"))
    covered = stateable - count["gap"]
    L = ["# glTF-Validator parity", "",
         "Every issue code the Khronos glTF-Validator can raise, and what the X3D checks in this repository do about the same mistake.",
         "This page is generated by `python tests/gltf_parity.py --md` from scenes that are run, not from reading the code: each X3D check named below",
         "has one scene that must be reported and one that must pass (`tests/test_gltf_parity.py`).", "",
         "Source of the code list: `ISSUES.md` in KhronosGroup/glTF-Validator (170 codes).", "",
         "## Count", "", "| What X3D does with the mistake | Codes |", "|---|---|",
         f"| The X3D 4.0 schema rejects it | {count['xsd']} |", f"| A semantic check that already existed reports it | {count['semantic']} |",
         f"| A check added in this work reports it | {count['new']} |", f"| The file does not parse | {count['parse']} |",
         f"| **Covered: {covered} of the {len(MAP)} Khronos codes (the {stateable} the project judged X3D can state; the project, not an outside party, classified the other {len(MAP) - stateable})** | **{covered}** |", f"| Not covered (gap) | {count['gap']} |",
         f"| Cannot be written: byte layout (buffers, views, GLB) | {count['binary']} |", f"| Cannot be written: X3D's design has no place for it | {count['design']} |",
         f"| glTF feature X3D 4.0 does not have | {count['feature']} |", f"| Total | {len(MAP)} |", "",
         "The byte-layout and design rows are not a win for the validator. They are a property of the format: the mistake has no spelling in X3D XML.",
         "The feature rows are the opposite: glTF materials (clearcoat, iridescence, volume, variants) that X3D 4.0 cannot express at all.", "",
         "## Checks X3D has that glTF-Validator has no code for", ""]
    for k, v in BEYOND.items():
        L.append(f"- `{F[k][1]}`: {v}")
    L += ["- ROUTE checks with no glTF counterpart: `route-wrong-access-type`, `route-never-fires`, `route-out-of-scope`, `route-before-def`",
          "- `containerfield-*`, `geo-coordinate-range`, `lod-range-order`, `hanim-joint-name`, rigid-body and Gaussian-splat checks", "",
          "## Every code", "", "| glTF-Validator code | X3D | X3D check | Proof scene | Note |", "|---|---|---|---|---|"]
    for code in sorted(MAP):
        _, fx, note = MAP[code]
        status = status_of(code, results)
        check = ""
        if fx:
            by, cid, *_ = F[fx]
            r = results[fx]
            check = f"`{cid}`" if cid and not (by == "any" and r.get("bad_by") == "xsd") else "XSD"
            if status == "parse":
                check = "XML parser"
        L.append(f"| {code} | {status} | {check} | {fx or ''} | {note} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    R = run_all()
    bad = [n for n, r in R.items() if not r["bad"] or r["good"]]
    if "--json" in sys.argv:
        can = [c for c, v in MAP.items() if v[1] and v[0] != "x3d-4.1"]
        miss = sorted(c for c in can if MAP[c][1] in bad)
        print(json.dumps({"codes": len(MAP), "stateable": len(can), "covered": len(can) - len(miss), "uncovered": miss,
                          "fixtures": len(F), "fixtures_wrong": sorted(bad)}))
        sys.exit(0)
    if "--md" in sys.argv:
        (ROOT / "docs" / "GLTF-VALIDATOR-PARITY.md").write_text(markdown(R))
    for n, r in R.items():
        flag = "ok " if n not in bad else "BAD"
        print(flag, n.ljust(24), "bad:", r["bad"], r.get("bad_by", ""), r["bad_all"], "| good:", r["good"], r["good_all"])
    print(len(F), "fixtures,", len(bad), "wrong;", len(MAP), "codes;", counts(R))
    sys.exit(1 if bad else 0)
