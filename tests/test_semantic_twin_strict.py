"""The heritage twin's strict shapes catch the eight planted faults its generated shapes pass.

Fixtures: examples/semantic_twins/data/heritage/{shapes.ttl, shapes-strict.ttl, mutants.json}.
"""

import json
from pathlib import Path

import pytest

from x3d_utils import semantic_web as sw

TWIN = Path(__file__).resolve().parent.parent / "examples" / "semantic_twins"
BASE = "https://example.org/scen/heritage/scene.ttl#"
MUTANTS = json.loads((TWIN / "data/heritage/mutants.json").read_text())["mutants"]


def _read(rel: str) -> str:
    return (TWIN / rel).read_text()


def _fired(shapes: str, scene: str, domain: str) -> list[str]:
    report = sw.shacl(
        sw.convert(scene, base_iri=BASE), shapes="none",
        extra_shapes_ttl=_read(shapes), data_ttl=domain,
    )
    return sorted({r["shape"].rsplit("#")[-1].rsplit(":")[-1] for r in report["results"]})


def test_clean_twin_conforms_to_strict_shapes():
    assert _fired("data/heritage/shapes-strict.ttl",
                  _read("scenes/heritage.x3d"), _read("data/heritage/domain.ttl")) == []


def test_shipped_faults_still_fire():
    assert _fired("data/heritage/shapes-strict.ttl", _read("scenes/heritage-fault.x3d"),
                  _read("data/heritage/domain-fault.ttl")) == ["S_H1s", "S_H2s"]


@pytest.mark.parametrize("m", MUTANTS, ids=[m["id"] for m in MUTANTS])
def test_planted_fault(m):
    scene, domain = _read("scenes/heritage.x3d"), _read("data/heritage/domain.ttl")
    src = scene if m["file"] == "scene" else domain
    assert m["find"] in src, "the mutant no longer applies to the fixture"
    out = src.replace(m["find"], m["replace"]) if m.get("all") else src.replace(m["find"], m["replace"], 1)
    if m["file"] == "scene":
        scene = out
    else:
        domain = out
    # The generated shapes pass it (the gap this file records) ...
    assert _fired("data/heritage/shapes.ttl", scene, domain) == []
    # ... and the strict shapes name it.
    assert _fired("data/heritage/shapes-strict.ttl", scene, domain) == sorted(m["expect"])
