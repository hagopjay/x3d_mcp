"""Every X3D check claimed in the USD validation parity map reports its failing scene and passes its passing
scene, the map covers every rule of the three pinned USD rulebooks, and the published table matches a fresh run."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
import usd_parity as up  # noqa: E402


@pytest.fixture(scope="module")
def results():
    return up.run_all()


@pytest.mark.parametrize("name", sorted(up.F))
def test_failing_scene_is_reported_and_passing_scene_is_not(name, results):
    r = results[name]
    assert r["bad"], f"{name}: the failing scene was not reported ({r['bad_all']})"
    assert not r["good"], f"{name}: the passing scene was reported ({r['good_all']})"


def test_map_covers_every_rule_and_names_real_fixtures():
    ids = [r["id"] for r in up.RULES["rules"]]
    assert len(ids) == len(set(ids)) == 340
    assert set(up.MAP) == set(ids)
    for rid, (status, fixture, _) in up.MAP.items():
        assert status in up.ORDER, rid
        if status in up.STATEABLE and status != "gap":
            assert fixture in up.F, rid
        else:
            assert fixture is None, rid


def test_rulebook_pins_are_recorded():
    pins = up.RULES["pins"]
    assert pins["usd-core"].startswith("0.26.") and pins["omniverse-asset-validator"] == "1.18.0"
    assert "c3bd1d8" in pins["simready-foundation"]


def test_no_gap_is_hidden(results):
    c = up.counts(results)
    assert c["gap"] == 0
    assert sum(c.values()) == 340


def test_every_new_check_is_claimed():
    """Each check in usd_checks.py answers at least one USD rule in the map, with a proof scene."""
    import inspect
    import re

    from validation import usd_checks
    claimed = {up.F[fx][1] for _, fx, _ in up.MAP.values() if fx}
    for fn in usd_checks.USD_SCENE_CHECKS + usd_checks.USD_FILE_CHECKS:
        ids = set(re.findall(r'_diag\("(?:error|warning|info)", "([a-z0-9-]+)"', inspect.getsource(fn)))
        assert ids and ids <= claimed, (fn.__name__, ids - claimed)


def test_published_table_is_current(results):
    assert (up.ROOT / "docs" / "USD-VALIDATION-PARITY.md").read_text() == up.markdown(results)
