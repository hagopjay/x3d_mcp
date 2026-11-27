"""The X3D SimReady profile checks (src/validation/simready_checks.py): silent unless a file opts in, and exact about
what each one reads. The failing/passing pair for every SimReady rule they answer is in tests/usd_parity.py."""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usd_parity as up  # noqa: E402
from gltf_parity import doc, findings  # noqa: E402


def sim(text, folder=None, levels=("error", "warning", "info")):
    _, xsd, ds = findings(text, folder)
    assert xsd == 0, "the scene must be valid X3D 4.0"
    return sorted(c for lv, c in ds if c.startswith("simready-") and lv in levels)


WORST = up.world(up.sr_body(geo="<Sphere radius='0.5'/>", attrs="fixed='true'"))
CM = "<unit category='length' name='centimetre' conversionFactor='0.01'/>"


def test_a_file_that_does_not_opt_in_is_never_reported():
    assert sim(doc(WORST, head=CM)) == []


def test_the_same_file_opted_in_is_reported_for_each_open_question():
    got = sim(doc(WORST, head=CM + '<meta name="simready:features" content="rigid_bodies multibody semantic_labels material_labels"/>'))
    assert got == ["simready-geometry-not-mesh", "simready-material-unlabelled", "simready-metadata-missing",
                   "simready-no-rigid-body", "simready-shape-unlabelled", "simready-single-body", "simready-thumbnail-missing",
                   "simready-unit-not-metres"]


def test_a_feature_not_claimed_is_not_asked():
    assert sim(up.sr_scene(up.sr_body(attrs="fixed='true'"), Image="pic.png")) == []


def test_labels_are_judged_without_opting_in():
    assert sim(doc(up.world(up.sr_body(shape_meta=up.labels(coco="gizmo"))))) == ["simready-label-not-in-vocabulary"]


def test_a_non_canonical_spelling_is_a_warning_as_in_simready_and_names_the_canonical_one():
    from lxml import etree
    from validation.simready_checks import check_simready_labels
    text = doc(up.world(up.sr_body(shape_meta=up.labels(cityscapes="Traffic_Light"))))
    scene = etree.fromstring(text.encode()).find("Scene")
    ds = check_simready_labels(scene)
    assert [(d.level, d.check) for d in ds] == [("warning", "simready-label-not-in-vocabulary")]
    assert "'traffic light'" in ds[0].message


def test_a_label_above_the_shape_or_on_its_material_covers_it():
    feat = {"simready__features": "semantic_labels"}
    on_body = up.sr_scene(f"<RigidBody DEF='A'>{up.labels(wikidata_qcode='Q111')}<CollidableShape containerField='geometry'>"
                          f"<Shape containerField='shape'>{up.APP}{up.MESH}</Shape></CollidableShape></RigidBody>", **feat)
    on_material = up.sr_scene(up.sr_body(app=f"<Appearance><Material>{up.labels(material='steel')}</Material></Appearance>"), **feat)
    assert "simready-shape-unlabelled" not in sim(on_body)
    assert "simready-shape-unlabelled" not in sim(on_material)


def test_a_malformed_head_qcode_is_reported():
    assert "simready-qcode-malformed" in sim(up.sr_scene(simready__qcode="toy"))


def test_thumbnail_named_and_absent_is_reported_only_with_a_folder(tmp_path):
    text = up.sr_scene(Image="absent.png")
    assert "simready-thumbnail-missing" not in sim(text)
    assert "simready-thumbnail-missing" in sim(text, tmp_path)
    (tmp_path / "absent.png").write_bytes(up.IMG["tex.png"])
    assert "simready-thumbnail-missing" not in sim(text, tmp_path)
    assert "simready-thumbnail-missing" in sim(up.sr_scene())


def test_vocabularies_are_generated_from_the_pinned_rulebook():
    from validation.simready_vocab import SOURCE_COMMIT, VOCAB
    assert SOURCE_COMMIT[:7] in up.RULES["pins"]["simready-foundation"]
    assert {k: len(v["names"]) for k, v in VOCAB.items()} == {
        "ade20k": 150, "cityscapes": 35, "coco": 80, "imagenet_1k": 998, "pascal_voc": 20, "sunrgbd": 10,
        "affordance": 9, "affordance_state": 12}


def test_the_ledger_counts_the_profile_apart(results=None):
    c = up.counts(up.run_all())
    assert sum(c[k] for k in up.STATEABLE) == 66 and c["profile"] == 20 and c["gap"] == 0


def test_a_mesh_beside_a_primitive_is_one_warning_however_many_primitives():
    two = up.sr_scene(up.sr_body("A"), up.sr_body("B", 2, geo="<Sphere radius='0.5'/>"), up.sr_body("C", 4, geo="<Box/>"), Image="p.png")
    assert sim(two) == ["simready-geometry-not-mesh"]


def test_untriangulated_is_information_only():
    quad = up.sr_scene(up.sr_body(geo=f"<IndexedFaceSet coordIndex='0 1 2 3 -1'>{up.QUAD}</IndexedFaceSet>"), Image="p.png")
    assert sim(quad, levels=("error", "warning")) == [] and sim(quad) == ["simready-mesh-not-triangulated"]
