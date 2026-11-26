"""Every X3D check claimed in the glTF-Validator parity map reports its failing scene and passes its
passing scene, the map covers all 170 glTF-Validator codes, and the published table matches a fresh run."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import gltf_parity as gp  # noqa: E402


@pytest.fixture(scope="module")
def results():
    return gp.run_all()


@pytest.mark.parametrize("name", sorted(gp.F))
def test_failing_scene_is_reported_and_passing_scene_is_not(name, results):
    r = results[name]
    assert r["bad"], f"{name}: the failing scene was not reported ({r['bad_all']})"
    assert not r["good"], f"{name}: the passing scene was reported ({r['good_all']})"


def test_map_covers_every_gltf_validator_code_and_names_real_fixtures():
    assert len(gp.MAP) == 170
    for code, (status, fixture, _) in gp.MAP.items():
        if status in ("binary", "design", "feature", "gap"):
            assert fixture is None, code
        else:
            assert fixture in gp.F, code


def test_no_gap_is_hidden(results):
    c = gp.counts(results)
    assert c["gap"] == 0
    assert sum(c.values()) == 170


def test_published_table_is_current(results):
    assert (gp.ROOT / "docs" / "GLTF-VALIDATOR-PARITY.md").read_text() == gp.markdown(results)


def test_file_checks_never_leave_the_scene_folder(tmp_path):
    from validation import semantic
    (tmp_path / "scene").mkdir()
    (tmp_path / "secret.png").write_bytes(b"not an image")
    text = gp.doc(gp.tex('"../secret.png"'))
    found = [d.check for d in semantic.semantic_diagnostics_with_files(text, tmp_path / "scene")]
    assert "image-unrecognized" not in found


def test_plain_call_reads_no_files():
    from validation import semantic
    assert "url-unresolved" not in [d.check for d in semantic.semantic_diagnostics(gp.doc(gp.tex('"gone.png"')))]
