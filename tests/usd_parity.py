"""USD validation parity: every rule of the three USD rulebooks (tests/usd_rules.json), mapped to what the X3D
checks in this repository do about the same mistake, with one failing and one passing scene for each X3D
check that is claimed.

    python tests/usd_parity.py            run every fixture, print the result table
    python tests/usd_parity.py --md       write docs/USD-VALIDATION-PARITY.md from the executed results
    python tests/usd_parity.py --json     print a summary as JSON

The rulebooks, pinned in tests/usd_rules.json:
    usd-core 26.11                       30 registered UsdValidation validators (Pixar OpenUSD)
    omniverse-asset-validator 1.18.0     40 rules (NVIDIA, PyPI)
    NVIDIA/simready-foundation c3bd1d8   270 requirements (SimReady Foundation; what Isaac Sim validates against)

Status words used in MAP. The first six are mistakes X3D can state:
    xsd        the X3D 4.0 schema rejects it
    semantic   a check that existed before this work reports it
    new        a check added for this work reports it (src/validation/usd_checks.py)
    parse      the file does not parse
    x3d-4.1    stated with an X3D 4.1 node (GaussianSplats); the check exists and is tested
    gap        X3D can state it and nothing here reports it
and the rest are not a validator matter in X3D, each for a stated reason:
    fixed      X3D's standard fixes the answer, so the mistake cannot be made (one root, +Y up, metres, seconds,
               normals computed when absent, a defined default appearance)
    convention SimReady fixes it differently from X3D (Z up); the X3D-to-USD exporter must convert
    design     the USD structure has no counterpart in X3D (layers, variants, payloads, kinds, overs, subsets,
               API schemas, relationships)
    binary     usdc/usdz byte layout and packaging
    feature    a USD or Omniverse feature X3D 4.0 does not have (MDL, MaterialX, articulations, lidar, render products,
               texture colour space)
    engine     tied to one simulator's runtime schema (PhysX, Newton, MuJoCo, Isaac Sim, OmniGraph)
    policy     a pipeline or profile convention (names, folders, thumbnails, required metadata keys, label
               vocabularies), checkable only against a profile X3D does not yet define
    judgement  needs a person or a render (tessellation density, pivot placement, logical grouping)
"""
from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gltf_parity as gp  # noqa: E402  (shares the scene helpers and the runner)
from gltf_parity import APP, QUAD, doc, findings, ifs, png  # noqa: E402

ROOT = gp.ROOT
RULES = json.loads((Path(__file__).resolve().parent / "usd_rules.json").read_text())

TRI = "<Coordinate point='0 0 0 1 0 0 0 1 0'/>"
TET = "<Coordinate point='0 0 0 1 0 0 0 1 0 0 0 1'/>"
TET_IDX = "0 2 1 -1 0 1 3 -1 0 3 2 -1 1 2 3 -1"


def shape(geo, app=APP):
    return f"<Shape>{app}{geo}</Shape>"


def body(name, x=0, extra="", collider=True, attrs=""):
    col = (f"<CollidableShape containerField='geometry'><Shape containerField='shape'>{APP}<Sphere radius='0.5'/></Shape>"
           f"</CollidableShape>") if collider else ""
    return f"<RigidBody DEF='{name}' position='{x} 0 0' {attrs}>{col}{extra}</RigidBody>"


def joint(a, b, kind="BallJoint"):
    return (f"<{kind} containerField='joints'><RigidBody USE='{a}' containerField='body1'/>"
            f"<RigidBody USE='{b}' containerField='body2'/></{kind}>")


def world(*kids, collide=False):
    cc = "<CollisionCollection containerField='collider'/>" if collide else ""
    return f"<RigidBodyCollection>{cc}{''.join(kids)}</RigidBodyCollection>"


def splat(attrs):
    base = dict(positions="0 0 0 1 0 0", orientations="0 0 0 1 0 0 0 1", scales="0.1 0.1 0.1 0.1 0.1 0.1",
                opacities="0.5 0.5", sphericalHarmonicsDegree0Coef0="0.1 0.1 0.1 0.2 0.2 0.2")
    base.update(attrs)
    a = " ".join(f"{k}='{v}'" for k, v in base.items() if v is not None)
    return doc(f"<GaussianSplats {a}/>", version="4.1")


IMG = dict(gp.IMG)
IMG["huge.png"] = png(w=20000, h=1)

# name: (by, check id or None, failing scene, passing scene)
# by: xsd | any | sem | sem-info | file | file-info | parse   (as in gltf_parity)
F = {
    # ---- the schema
    "type": gp.F["type"],
    "float": gp.F["float"],
    "unknown-node": ("xsd", None, doc(shape("<Box/>") + "<Gizmo/>"), doc(shape("<Box/>"))),
    "nested-geometry": ("xsd", None, doc(f"<Shape>{APP}<Box><Sphere/></Box></Shape>"), doc(shape("<Box/>"))),
    "nested-material": ("xsd", None, doc("<Shape><Appearance><Material><Material/></Material></Appearance><Box/></Shape>"),
                        doc(shape("<Box/>"))),
    "pbr-range": ("any", None, doc(shape("<Box/>", "<Appearance><PhysicalMaterial roughness='2'/></Appearance>")),
                  doc(shape("<Box/>", "<Appearance><PhysicalMaterial roughness='0.5'/></Appearance>"))),
    "colour-range": ("any", None, doc(ifs("coordIndex='0 1 2 3 -1'", QUAD + "<Color color='2 0 0 1 0 0 1 0 0 1 0 0'/>")),
                     doc(ifs("coordIndex='0 1 2 3 -1'", QUAD + "<Color color='1 0 0 1 0 0 1 0 0 1 0 0'/>"))),
    "alpha-range": ("any", None, doc(ifs("coordIndex='0 1 2 3 -1'", QUAD + "<ColorRGBA color='1 0 0 1.5 1 0 0 1 1 0 0 1 1 0 0 1'/>")),
                    doc(ifs("coordIndex='0 1 2 3 -1'", QUAD + "<ColorRGBA color='1 0 0 0.5 1 0 0 1 1 0 0 1 1 0 0 1'/>"))),
    # ---- checks that existed before this work
    "normal-unit": ("sem", "normal-not-unit", doc(ifs(kids=QUAD + "<Normal vector='0 0 2 0 0 1 0 0 1 0 0 1'/>")),
                    doc(ifs(kids=QUAD + "<Normal vector='0 0 1 0 0 1 0 0 1 0 0 1'/>"))),
    "index-oob": gp.F["index-oob"],
    "bbox": gp.F["bbox"],
    "use-undefined": ("any", "use-undefined-def", doc(f"<Shape><Appearance USE='Missing'/><Box/></Shape>"),
                      doc(f"<Shape><Appearance DEF='Look'><Material/></Appearance><Box/></Shape><Shape><Appearance USE='Look'/><Box/></Shape>")),
    "url-missing": ("file", "url-unresolved", doc(gp.tex('"absent.png"')), doc(gp.tex('"tex.png"'))),
    "not-image": ("file", "image-unrecognized", doc(gp.tex('"notes.png"')), doc(gp.tex('"tex.png"'))),
    "url-absolute": ("sem", "url-absolute-path", doc(gp.tex('"file:///C:/Users/me/tex.png"')), doc(gp.tex('"tex.png"'))),
    "proto-undeclared": ("any", "proto-undeclared", doc("<ProtoInstance name='Gear'/>" + shape("<Box/>")),
                         doc("<ProtoDeclare name='Gear'><ProtoBody><Group/></ProtoBody></ProtoDeclare><ProtoInstance name='Gear'/>" + shape("<Box/>"))),
    "physics-mass": ("sem", "physics-mass", doc(world(body("A", attrs="mass='-1'"))), doc(world(body("A", attrs="mass='2'")))),
    "joint-body": ("sem", "physics-joint-body",
                   doc(world(body("A"), body("B", 2), "<BallJoint containerField='joints'><RigidBody USE='A' containerField='body1'/></BallJoint>")),
                   doc(world(body("A"), body("B", 2), joint("A", "B")))),
    "joint-two-targets": ("any", "sfnode-multiple",
                          doc(world(body("A"), body("B", 2), body("C", 4),
                                    "<BallJoint containerField='joints'><RigidBody USE='A' containerField='body1'/>"
                                    "<RigidBody USE='C' containerField='body1'/><RigidBody USE='B' containerField='body2'/></BallJoint>")),
                          doc(world(body("A"), body("B", 2), joint("A", "B")))),
    # ---- X3D 4.1 GaussianSplats (checks existed; the node is not in the 4.0 schema)
    "splat-positions": ("sem", "splat-array-length", splat({"positions": "0 0 0"}), splat({})),
    "splat-scales": ("sem", "splat-array-length", splat({"scales": "0.1 0.1 0.1"}), splat({})),
    "splat-orientations": ("sem", "splat-array-length", splat({"orientations": "0 0 0 1"}), splat({})),
    "splat-opacities": ("sem", "splat-array-length", splat({"opacities": "0.5"}), splat({})),
    "splat-radiance": ("sem", "splat-no-color", splat({"sphericalHarmonicsDegree0Coef0": None}), splat({})),
    # ---- checks added for this work (src/validation/usd_checks.py)
    "manifold": ("sem", "mesh-non-manifold-edge",
                 doc(ifs("coordIndex='0 1 2 -1 0 1 3 -1 0 1 4 -1'", "<Coordinate point='0 0 0 1 0 0 0 1 0 0 -1 0 0 0 1'/>")),
                 doc(ifs("coordIndex='0 1 2 -1 1 0 3 -1'", "<Coordinate point='0 0 0 1 0 0 0 1 0 0 -1 0'/>"))),
    "zero-area": ("sem", "mesh-zero-area-face", doc(ifs("coordIndex='0 1 2 -1'", "<Coordinate point='0 0 0 1 0 0 2 0 0'/>")),
                  doc(ifs("coordIndex='0 1 2 -1'", TRI))),
    "winding": ("sem", "mesh-normal-against-winding", doc(ifs("coordIndex='0 1 2 -1'", TRI + "<Normal vector='0 0 -1 0 0 -1 0 0 -1'/>")),
                doc(ifs("coordIndex='0 1 2 -1'", TRI + "<Normal vector='0 0 1 0 0 1 0 0 1'/>"))),
    "unused-point": ("sem-info", "mesh-unused-point", doc(ifs("coordIndex='0 1 2 -1'", "<Coordinate point='0 0 0 1 0 0 0 1 0 5 5 5'/>")),
                     doc(ifs("coordIndex='0 1 2 -1'", TRI))),
    "unused-value": ("sem-info", "mesh-unused-value",
                     doc(ifs("coordIndex='0 1 2 3 -1' colorIndex='0 0 0 0 -1'", QUAD + "<Color color='1 0 0 0 1 0'/>")),
                     doc(ifs("coordIndex='0 1 2 3 -1' colorIndex='0 1 0 1 -1'", QUAD + "<Color color='1 0 0 0 1 0'/>"))),
    "colocated": ("sem-info", "mesh-colocated-points", doc(ifs("coordIndex='0 1 2 -1'", "<Coordinate point='0 0 0 1 0 0 0 0 0'/>")),
                  doc(ifs("coordIndex='0 1 2 -1'", TRI))),
    "precision": ("sem", "coordinate-precision", doc(ifs("coordIndex='0 1 2 -1'", "<Coordinate point='100000 0 0 100001 0 0 100000 1 0'/>")),
                  doc(ifs("coordIndex='0 1 2 -1'", TRI))),
    "no-geometry": ("sem-info", "scene-no-geometry", doc("<WorldInfo title='empty'/>"), doc(shape("<Box/>"))),
    "constant-interp": ("sem-info", "interpolator-constant", doc(gp.anim(key_value="1 1 1 1 1 1")), doc(gp.anim())),
    "transparent-open": ("sem-info", "transparent-open-mesh",
                         doc(f"<Shape><Appearance><Material transparency='0.5'/></Appearance><IndexedFaceSet coordIndex='0 1 2 3 -1'>{QUAD}</IndexedFaceSet></Shape>"),
                         doc(f"<Shape><Appearance><Material transparency='0.5'/></Appearance><IndexedFaceSet coordIndex='{TET_IDX}'>{TET}</IndexedFaceSet></Shape>")),
    "coincident": ("sem-info", "mesh-coincident", doc(f"<Group>{shape('<Box/>')}{shape('<Box/>')}</Group>"),
                   doc(f"<Group>{shape('<Box/>')}{shape(chr(60) + 'Sphere/>')}</Group>")),
    "unicode-name": ("sem", "def-name-unicode-ambiguous",
                     doc(f"<Transform DEF='Caf\u00e9'>{shape('<Box/>')}</Transform><Transform DEF='Cafe\u0301'>{shape('<Box/>')}</Transform>"),
                     doc(f"<Transform DEF='Cafe1'>{shape('<Box/>')}</Transform><Transform DEF='Cafe2'>{shape('<Box/>')}</Transform>")),
    "inertia": ("sem", "physics-inertia", doc(world(body("A", attrs="inertia='1 0 0 0 1 0 0 0 5'"))),
                doc(world(body("A", attrs="inertia='1 0 0 0 1 0 0 0 1'")))),
    "body-shared": ("sem", "physics-body-shared", doc(world(body("A"), "<RigidBody USE='A'/>")), doc(world(body("A"), body("B", 2)))),
    "joint-loop": ("sem-info", "physics-joint-loop", doc(world(body("A"), body("B", 2), joint("A", "B"), joint("A", "B"))),
                   doc(world(body("A"), body("B", 2), joint("A", "B")))),
    "no-collider": ("sem-info", "physics-body-no-collider", doc(world(body("A", collider=False), collide=True)),
                    doc(world(body("A"), collide=True))),
    "collider-overlap": ("sem-info", "physics-collider-overlap", doc(world(body("A"), body("B", 0.5))),
                         doc(world(body("A"), body("B", 2)))),
    "texture-large": ("file", "texture-too-large", doc(gp.tex('"huge.png"')), doc(gp.tex('"tex.png"'))),
}

# rule id -> (status, fixture or None, note)
X, S, N, G, P = "xsd", "semantic", "new", "gap", "parse"
FIX, CONV, D, B, FEAT, ENG, POL, J, X41 = "fixed", "convention", "design", "binary", "feature", "engine", "policy", "judgement", "x3d-4.1"

MAP: dict[str, tuple[str, str | None, str]] = {
    # ---- usd-core 26.11 (Pixar)
    "usd:usdGeomValidators:EncapsulationChecker": (X, "nested-geometry", "a geometry node holds no other geometry"),
    "usd:usdGeomValidators:StageMetadataChecker": (FIX, None, "X3D is +Y up and in metres unless a UNIT statement says otherwise"),
    "usd:usdGeomValidators:SubsetFamilies": (D, None, "no GeomSubset: X3D splits a mesh into Shapes"),
    "usd:usdGeomValidators:SubsetParentIsImageable": (D, None, "no GeomSubset"),
    "usd:usdLuxValidators:EncapsulationRulesValidator": (D, None, "no connectable-prim nesting"),
    "usd:usdMediaValidators:AuthorshipInputsPaired": (D, None, "no authorship records"),
    "usd:usdMediaValidators:ShadowedOrDuplicateAuthorship": (D, None, "no authorship records"),
    "usd:usdPhysicsValidators:ArticulationChecker": (FEAT, None, "X3D 4.0 has no articulation root"),
    "usd:usdPhysicsValidators:ColliderChecker": (FIX, None, "CollidableShape has translation and rotation, no scale"),
    "usd:usdPhysicsValidators:PhysicsJointChecker": (S, "joint-body", ""),
    "usd:usdPhysicsValidators:RigidBodyChecker": (N, "body-shared", "instancing; nesting and xformability are fixed in X3D"),
    "usd:usdShadeValidators:EncapsulationMaterialValidator": (D, None, "no shading network prims"),
    "usd:usdShadeValidators:EncapsulationRulesValidator": (D, None, "no shading network prims"),
    "usd:usdShadeValidators:MaterialBindingApiAppliedValidator": (D, None, "no API schemas"),
    "usd:usdShadeValidators:MaterialBindingCollectionValidator": (D, None, "no collections"),
    "usd:usdShadeValidators:MaterialBindingRelationships": (D, None, "no relationships: Appearance is a child"),
    "usd:usdShadeValidators:NormalMapTextureValidator": (FIX, None, "normalTexture decoding (2c-1) is fixed by 19775-1 12.4.2"),
    "usd:usdShadeValidators:ShaderSdrCompliance": (D, None, "no shader registry"),
    "usd:usdShadeValidators:SubsetMaterialBindFamilyName": (D, None, "no GeomSubset"),
    "usd:usdShadeValidators:SubsetsMaterialBindFamily": (D, None, "no GeomSubset"),
    "usd:usdSkelValidators:SkelBindingApiAppliedValidator": (D, None, "no API schemas"),
    "usd:usdSkelValidators:SkelBindingApiValidator": (FIX, None, "skin Shapes live in HAnimHumanoid.skin by schema"),
    "usd:usdUtilsValidators:FileExtensionValidator": (S, "not-image", "texture content is sniffed, not only the name"),
    "usd:usdUtilsValidators:MissingReferenceValidator": (S, "url-missing", ""),
    "usd:usdUtilsValidators:PackageEncapsulationValidator": (B, None, "usdz package"),
    "usd:usdUtilsValidators:RootPackageValidator": (B, None, "usdz package"),
    "usd:usdUtilsValidators:UsdzPackageValidator": (B, None, "usdz package"),
    "usd:usdValidation:AttributeTypeMismatch": (X, "type", ""),
    "usd:usdValidation:CompositionErrorTest": (S, "proto-undeclared", "X3D's composition is Inline and PROTO; url-unresolved covers Inline"),
    "usd:usdValidation:StageMetadataChecker": (FIX, None, "one Scene root: there is no defaultPrim to forget"),
    # ---- omniverse-asset-validator 1.18.0 (NVIDIA)
    "oav:UsdzPackageValidator": (B, None, "usdz package"),
    "oav:MissingReferenceChecker": (S, "url-missing", ""),
    "oav:StageMetadataChecker": (FIX, None, "upAxis, metersPerUnit and defaultPrim are fixed in X3D"),
    "oav:TextureChecker": (S, "not-image", ""),
    "oav:PrimEncapsulationChecker": (X, "nested-geometry", ""),
    "oav:NormalMapTextureChecker": (FIX, None, "normalTexture decoding is fixed"),
    "oav:KindChecker": (D, None, "no model kinds"),
    "oav:ExtentsChecker": (S, "bbox", "bboxSize is optional in X3D; when given it must enclose"),
    "oav:TypeChecker": (X, "unknown-node", "every element is a typed node"),
    "oav:IndexedPrimvarChecker": (FIX, None, "X3D attributes are indexed by construction (coordIndex, colorIndex)"),
    "oav:ManifoldChecker": (N, "manifold", ""),
    "oav:NormalsExistChecker": (FIX, None, "absent normals are computed from creaseAngle (19775-1 11.2.3)"),
    "oav:NormalsValidChecker": (S, "normal-unit", "counts: attribute-count-mismatch"),
    "oav:NormalsWindingsChecker": (N, "winding", ""),
    "oav:SubdivisionSchemeChecker": (D, None, "no subdivision schemes"),
    "oav:UnusedMeshTopologyChecker": (N, "unused-point", ""),
    "oav:UnusedPrimvarChecker": (N, "unused-value", ""),
    "oav:ValidateTopologyChecker": (S, "index-oob", "also face-too-few-vertices, degenerate-face"),
    "oav:WeldChecker": (N, "colocated", ""),
    "oav:ZeroAreaFaceChecker": (N, "zero-area", "degenerate-face covers a repeated index"),
    "oav:LayerSpecChecker": (X, "type", ""),
    "oav:UsdAsciiPerformanceChecker": (D, None, "X3D's answer to large text arrays is its binary and compressed encodings"),
    "oav:DefaultPrimChecker": (FIX, None, "one Scene root"),
    "oav:DanglingOverPrimChecker": (D, None, "no over specifiers"),
    "oav:MaterialPathChecker": (FEAT, None, "MDL"),
    "oav:MaterialOutOfScopeChecker": (FIX, None, "a USE cannot reach across an Inline boundary"),
    "oav:UsdDanglingMaterialBinding": (S, "use-undefined", ""),
    "oav:UsdMaterialBindingApi": (D, None, "no API schemas"),
    "oav:MaterialUsdPreviewSurfaceChecker": (X, "pbr-range", "PhysicalMaterial field types and ranges"),
    "oav:ShaderImplementationSourceChecker": (D, None, "no shader implementation source"),
    "oav:MaterialOldMdlSchemaChecker": (FEAT, None, "MDL"),
    "oav:UnicodeNameChecker": (N, "unicode-name", ""),
    "oav:UsdGeomSubsetChecker": (D, None, "no GeomSubset"),
    "oav:UsdLuxSchemaChecker": (D, None, "no inputs: namespace"),
    "oav:SkelBindingAPIAppliedChecker": (D, None, "no API schemas"),
    "oav:RigidBodyChecker": (N, "body-shared", ""),
    "oav:ColliderChecker": (FIX, None, "collidables carry no scale"),
    "oav:PhysicsJointChecker": (S, "joint-body", ""),
    "oav:ArticulationChecker": (FEAT, None, "no articulation root"),
    "oav:MassChecker": (N, "inertia", "mass sign: physics-mass"),
}

# SimReady: defaults by capability, then per-requirement entries
CAP_DEFAULT = {
    "aif": (POL, None, "AIF data-centre equipment profile"), "assembly": (POL, None, "SimReady mate and connection profile"),
    "core/asset_frame": (POL, None, "facing metadata"), "core/asset_variants": (D, None, "variant sets"),
    "core/naming_paths": (POL, None, "naming and folder convention"), "core/runtime_variants": (ENG, None, "per-simulator variant sets"),
    "core/sim_ready": (POL, None, "SimReady metadata and thumbnail"), "dataset_taxonomies": (POL, None, "label vocabulary"),
    "isaac_sim": (ENG, None, "Isaac Sim"), "nonvisual_sensors": (FEAT, None, "non-visual sensor materials and lidar"),
    "packaging": (POL, None, "package metadata files"), "physics_bodies/object_affordances": (POL, None, "affordance labels"),
    "physics_bodies/physics_colliders": (ENG, None, "collider approximation"), "physics_bodies/physics_graspable": (POL, None, "grasp annotation"),
    "physics_bodies/physics_grippers": (POL, None, "gripper annotation"), "physics_bodies/physics_sensors": (FEAT, None, "IMU and joint-state sensors"),
    "rendering": (FEAT, None, "render products"), "semantic_labels": (POL, None, "labels can ride in X3D metadata; no standard vocabulary"),
    "template": (FIX, None, "one Scene root"), "physics_bodies/physics_materials": (D, None, "contact parameters live on CollisionCollection"),
    "physics_bodies/base_articulation": (FEAT, None, "articulation"), "physics_bodies/physics_driven_joints": (ENG, None, "robot drive schema"),
    "physics_bodies/physics_joints": (FEAT, None, "articulation"), "physics_bodies/physics_rigid_bodies": (ENG, None, "simulator collision schema"),
}
SR = {
    "AA.001": (S, "url-absolute", "anchored (relative) paths"), "AA.002": (S, "not-image", ""),
    "atomic_asset/ov-usdz-udim-limitation": (B, None, "usdz"),
    "NP.007": (S, "url-absolute", ""), "NP.008": (S, "url-missing", ""),
    "UN.001": (FIX, None, "+Y up by definition"), "UN.002": (FIX, None, "metres unless a UNIT statement says otherwise"),
    "UN.003": (FIX, None, "kilograms unless a UNIT statement says otherwise"),
    "UN.004": (FIX, None, "(L) each scene's UNIT statements apply to its own content"),
    "UN.005": (FIX, None, "time is in seconds"), "UN.006": (CONV, None, "X3D is +Y up; SimReady wants Z: the exporter rotates (Blender's USD export writes Z)"),
    "UN.007": (POL, None, "a UNIT length other than 1 is legal X3D; SimReady wants metres"),
    "HI.001": (FIX, None, "one Scene root"), "HI.002": (POL, None, ""), "HI.003": (POL, None, ""), "HI.004": (FIX, None, "one Scene root"),
    "HI.005": (D, None, "no xformOp stacks"), "HI.006": (J, None, ""), "HI.007": (POL, None, "placeholder requirement"),
    "HI.008": (J, None, ""), "HI.009": (J, None, ""), "HI.010": (D, None, "no over specifiers"),
    "RM.001": (X, "nested-material", ""), "RM.002": (POL, None, "material scope convention"),
    "OPT.001": (FEAT, None, "X3D 4.0 has no fixed joint"), "OPT.002": (J, None, ""), "OPT.003": (D, None, "no API schemas"),
    "OPT.004": (FEAT, None, "MaterialX"),
    "BA.001": (FEAT, None, "articulation root"), "BA.002": (N, "collider-overlap", "primitive collidables: spheres exact, others as oriented boxes"),
    "DJ.006": (J, None, "what is reasonable depends on the robot"), "DJ.011": (N, "joint-loop", ""),
    "JT.001": (J, None, "free-floating is a design choice"), "JT.002": (S, "joint-body", ""), "JT.003": (S, "joint-two-targets", ""),
    "RB.001": (POL, None, "profile asks for at least one body"), "RB.003": (FIX, None, "RigidBody has position and orientation"),
    "RB.005": (N, "body-shared", ""), "RB.006": (FIX, None, "bodies are a flat list in RigidBodyCollection"),
    "RB.007": (FIX, None, "mass defaults to 1 kg"), "RB.008": (N, "inertia", ""),
    "RB.009": (FIX, None, "(L) a body's pose has no scale or skew field"), "RB.010": (FIX, None, "collidable geometry is not drawn"),
    "RB.011": (S, "physics-mass", ""), "RB.012": (FIX, None, "bodies cannot nest"), "RB.013": (N, "no-collider", ""),
    "physics_rigid_bodies/collider-capability": (D, None, "no API schemas"), "physics_rigid_bodies/collider-mesh": (D, None, "no API schemas"),
    "physics_rigid_bodies/collider-non-uniform-scale": (FIX, None, "collidables carry no scale"),
    "physics_rigid_bodies/mesh-collision-api": (D, None, "no API schemas"),
    "physics_rigid_bodies/rigid-body-multibody-capability": (POL, None, "profile asks for two bodies"),
    "physics_joints/articulation": (FEAT, None, "articulation"),
    "DISP.001": (FIX, None, "a Shape without Appearance is drawn unlit white"), "DISP.002": (X, "colour-range", ""),
    "DISP.003": (X, "alpha-range", "counts: attribute-count-mismatch"),
    "SPL.001": (POL, None, "the asset's chosen representation"),
    "SPL.002": (X41, "splat-positions", ""), "SPL.003": (X41, "splat-scales", ""), "SPL.004": (X41, "splat-orientations", ""),
    "SPL.005": (X41, "splat-opacities", ""), "SPL.006": (X41, "splat-radiance", ""),
    "VG.001": (N, "no-geometry", ""), "VG.002": (S, "bbox", ""), "VG.003": (J, None, ""), "VG.004": (J, None, ""),
    "VG.005": (J, None, ""), "VG.006": (J, None, ""), "VG.007": (N, "manifold", ""), "VG.008": (N, "coincident", "same geometry under one parent"),
    "VG.009": (FIX, None, "indexed by construction"), "VG.010": (D, None, "no subdivision schemes"), "VG.011": (N, "unused-value", ""),
    "VG.012": (J, None, ""), "VG.013": (J, None, ""), "VG.014": (S, "index-oob", ""), "VG.015": (N, "constant-interp", ""),
    "VG.016": (N, "colocated", ""), "VG.017": (J, None, ""), "VG.018": (N, "unused-point", ""), "VG.019": (N, "zero-area", ""),
    "VG.020": (N, "precision", "1 mm at the point's distance from the origin"), "VG.021": (POL, None, "polygons are legal X3D"),
    "VG.022": (N, "transparent-open", ""), "VG.023": (J, None, ""), "VG.024": (J, None, ""), "VG.025": (J, None, ""),
    "VG.026": (J, None, ""), "VG.027": (FIX, None, "normals are computed when absent"), "VG.028": (S, "normal-unit", ""),
    "VG.029": (N, "winding", ""), "geometry/geom-shall-be-mesh": (POL, None, "primitives are legal X3D"),
    "geometry/usdgeom-boundable-size-rtx-limit": (N, "precision", "partial: point values only; a far Transform is not followed"),
    "materials/material-assignment": (FIX, None, "a Shape without Appearance is drawn unlit white"),
    "materials/material-bind-scope": (FIX, None, "a USE cannot reach across an Inline boundary"),
    "materials/material-final-surface": (D, None, ""), "materials/material-mdl-schema": (FEAT, None, "MDL"),
    "materials/material-mdl-source-asset": (FEAT, None, "MDL"), "materials/material-mdl-surface-present": (FEAT, None, "MDL"),
    "materials/material-pbr-parameter-ranges": (X, "pbr-range", ""), "materials/material-preview-surface-present": (D, None, ""),
    "materials/material-preview-surface": (X, "type", ""), "materials/material-shader-inputs": (X, "float", ""),
    "materials/material-shading-network-structure": (D, None, "no shading networks"),
    "materials/material-texture-colorspace-mdl-deprecated": (FEAT, None, "texture colour space"),
    "materials/material-texture-colorspace-mdl": (FEAT, None, "texture colour space"),
    "materials/material-texture-colorspace-openpbr": (FEAT, None, "texture colour space"),
    "materials/material-texture-colorspace-preview": (FEAT, None, "texture colour space (an X_ITE extension, not X3D 4.0)"),
    "materials/material-texture-maxsize": (N, "texture-large", ""),
}
for r in RULES["rules"]:
    if r["source"] != "simready-foundation":
        continue
    rid = r["id"][3:]
    short = r["capability"].split("/")[-1] + "/" + r["name"]
    entry = SR.get(rid) or SR.get(short)
    if entry is None:
        cap = r["capability"]
        entry = CAP_DEFAULT.get(cap) or CAP_DEFAULT.get(cap.split("/")[0])
    if entry is None:
        raise SystemExit(f"unmapped SimReady requirement {r['id']} ({r['capability']})")
    MAP[r["id"]] = entry

STATEABLE = ("xsd", "semantic", "new", "parse", "x3d-4.1", "gap")
ORDER = ["xsd", "semantic", "new", "parse", "x3d-4.1", "gap", "fixed", "convention", "design", "binary", "feature", "engine",
         "policy", "judgement"]


def run_fixture(name: str, folder: Path) -> dict:
    by, check, bad, good = F[name]
    use_folder = folder if by.startswith("file") else None
    levels = ("error", "warning", "info") if by.endswith("-info") else ("error", "warning")
    res = {}
    for label, text in (("bad", bad), ("good", good)):
        ok, xsd, ds = findings(text, use_folder)
        hit = [c for lv, c in ds if lv in levels and (check is None or c == check)]
        if by == "parse":
            res[label] = not ok
        elif by == "xsd":
            res[label] = xsd > 0 and text.find('version="4.1"') < 0
        elif by == "any":
            res[label] = xsd > 0 or bool(hit)
            res[label + "_by"] = "xsd" if xsd else ("semantic" if hit else "")
        else:
            res[label] = bool(hit)
        res[label + "_all"] = (xsd, sorted({c for lv, c in ds if lv in ("error", "warning")}))
    return res


def run_all() -> dict:
    with tempfile.TemporaryDirectory() as td:
        folder = Path(td)
        for n, b in IMG.items():
            (folder / n).write_bytes(b)
        return {name: run_fixture(name, folder) for name in F}


def status_of(rid: str, results: dict) -> str:
    status, fx, _ = MAP[rid]
    if fx and F[fx][0] == "any" and status in ("semantic", "xsd"):
        return "xsd" if results[fx].get("bad_by") == "xsd" else "semantic"
    if fx and status in STATEABLE and not (results[fx]["bad"] and not results[fx]["good"]):
        return "gap"
    return status


def counts(results: dict, source: str | None = None) -> dict:
    ids = [r["id"] for r in RULES["rules"] if source is None or r["source"] == source]
    return {k: sum(1 for i in ids if status_of(i, results) == k) for k in ORDER}


def markdown(results: dict) -> str:
    srcs = [("usd-core", "OpenUSD (usd-core 26.11)"), ("omniverse-asset-validator", "Omniverse Asset Validator 1.18.0"),
            ("simready-foundation", "SimReady Foundation c3bd1d8")]
    per = {s: counts(results, s) for s, _ in srcs}
    tot = counts(results)
    rows = [("The X3D 4.0 schema rejects it", "xsd"), ("A check that existed before this work reports it", "semantic"),
            ("A check added in this work reports it", "new"), ("Stated with an X3D 4.1 node (GaussianSplats), checked", "x3d-4.1"),
            ("Not covered (gap)", "gap"), ("X3D's standard fixes the answer, so the mistake cannot be made", "fixed"),
            ("SimReady fixes it differently from X3D; the exporter converts", "convention"),
            ("USD structure with no X3D counterpart", "design"), ("usdc/usdz byte layout and packaging", "binary"),
            ("USD or Omniverse feature X3D 4.0 does not have", "feature"), ("One simulator's runtime schema", "engine"),
            ("Pipeline or profile convention", "policy"), ("Needs a person or a render", "judgement")]

    def stateable(c):
        return sum(c[k] for k in STATEABLE)

    L = ["# USD validation parity", "",
         "Every rule of the three USD rulebooks, and what the X3D checks in this repository do about the same mistake.",
         "This page is generated by `python tests/usd_parity.py --md` from scenes that are run, not from reading the code: each X3D check named",
         "below has one scene that must be reported and one that must pass (`tests/test_usd_parity.py`). The rule list is pinned in",
         "`tests/usd_rules.json`: " + ", ".join(f"{k} {v}" for k, v in RULES["pins"].items()) + ".", "",
         "## Count", "", "| What X3D does with the mistake | OpenUSD | Omniverse AV | SimReady | All |", "|---|---|---|---|---|"]
    for label, k in rows:
        L.append(f"| {label} | " + " | ".join(str(per[s][k]) for s, _ in srcs) + f" | {tot[k]} |")
    L.append("| **Covered, of the mistakes X3D can state** | " + " | ".join(
        f"**{stateable(per[s]) - per[s]['gap']} of {stateable(per[s])}**" for s, _ in srcs) +
        f" | **{stateable(tot) - tot['gap']} of {stateable(tot)}** |")
    L.append("| Rules | " + " | ".join(str(sum(per[s].values())) for s, _ in srcs) + f" | {sum(tot.values())} |")
    L += ["", "How to read it:", "",
          "- **Most USD rules are not about mistakes X3D can make.** OpenUSD's own validators are mostly about USD's composition",
          "  machinery (layers, API schemas, subsets, packages); X3D has none of it, so those rows are a property of the format, not a score.",
          "- **The fixed rows are the opposite of a gap.** Where USD needs a rule (declare the up axis, the unit, the default prim, compute",
          "  normals), X3D's standard fixes the answer, so an X3D file cannot get it wrong.",
          "- **The convention row is the exporter's job.** SimReady wants Z up; X3D is +Y up. Blender's USD export writes Z up, so the X3D →",
          "  Blender → USD road meets it; a direct X3D → USD writer must rotate.",
          "- **Engine and policy rows are where SimReady is a profile, not a validator.** They name PhysX, Newton, MuJoCo and Isaac Sim",
          "  schemas, file and folder layouts, thumbnails and label vocabularies. An X3D SimReady profile (metadata conventions checked here)",
          "  would move many policy rows into the covered column; it does not exist yet.",
          "- **The feature rows count against X3D as a language**: MDL and MaterialX materials, texture colour space, articulations, fixed",
          "  joints, lidar and render products cannot be written in X3D 4.0.", "",
          "## Every rule", "", "| Rule | Source | X3D | X3D check | Proof scene | Note |", "|---|---|---|---|---|---|"]
    short = {"usd-core": "OpenUSD", "omniverse-asset-validator": "Omniverse AV", "simready-foundation": "SimReady"}
    for r in RULES["rules"]:
        status, fx, note = MAP[r["id"]]
        st = status_of(r["id"], results)
        check = ""
        if fx:
            by, cid, *_ = F[fx]
            check = "XSD" if by == "xsd" or (by == "any" and results[fx].get("bad_by") == "xsd") else f"`{cid}`"
        name = r["id"].split(":", 1)[1]
        if r["source"] == "simready-foundation" and r.get("code"):
            name = f"{r['code']} {r['name']}"
        L.append(f"| {name} | {short[r['source']]} | {st} | {check} | {fx or ''} | {note} |")
    return "\n".join(L) + "\n"


if __name__ == "__main__":
    R = run_all()
    wrong = [n for n, r in R.items() if not r["bad"] or r["good"]]
    if "--json" in sys.argv:
        print(json.dumps({"rules": len(MAP), "counts": counts(R), "fixtures": len(F), "fixtures_wrong": sorted(wrong)}))
        sys.exit(0)
    if "--md" in sys.argv:
        (ROOT / "docs" / "USD-VALIDATION-PARITY.md").write_text(markdown(R))
    for n, r in R.items():
        print("ok " if n not in wrong else "BAD", n.ljust(20), "bad:", r["bad"], r.get("bad_by", ""), r["bad_all"], "| good:", r["good"], r["good_all"])
    print(len(F), "fixtures,", len(wrong), "wrong;", len(MAP), "rules;", counts(R))
    sys.exit(1 if wrong else 0)
