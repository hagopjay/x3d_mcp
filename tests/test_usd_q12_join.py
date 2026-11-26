"""The Q12 join classes are assigned as documented (no Blender or USD install needed: verdicts are synthetic)."""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent / "usd_q12"))
import join  # noqa: E402


def _run(tmp_path, usd_for_scene, monkeypatch, scene_text):
    scenes = tmp_path / "s"; scenes.mkdir()
    (scenes / "zero-area__bad.x3d").write_text(scene_text)
    exp = tmp_path / "e.json"; exp.write_text(json.dumps({"blender": "x", "scenes": {"zero-area__bad": {}}}))
    rb = tmp_path / "r.json"; rb.write_text(json.dumps({"versions": {}, "scenes": {"zero-area__bad": usd_for_scene}}))
    out = tmp_path / "o.json"
    join.main(str(scenes), str(exp), str(rb), str(out))
    return json.loads(out.read_text())


def test_classes(tmp_path, monkeypatch):
    bad = join.u.F["zero-area"][2]
    r = _run(tmp_path, {"oav:ZeroAreaFaceChecker": "fail"}, monkeypatch, bad)
    assert r["per_rule"]["oav:ZeroAreaFaceChecker"] == {"agree-fail": 1}
    (tmp_path / "b").mkdir()
    r = _run(tmp_path / "b", {"oav:ZeroAreaFaceChecker": "pass"}, monkeypatch, bad)
    assert r["per_rule"]["oav:ZeroAreaFaceChecker"] == {"x3d-only": 1}
    good = join.u.F["zero-area"][3]
    (tmp_path / "c").mkdir()
    r = _run(tmp_path / "c", {"oav:ZeroAreaFaceChecker": "fail"}, monkeypatch, good)
    assert r["per_rule"]["oav:ZeroAreaFaceChecker"] == {"export-made": 1}
