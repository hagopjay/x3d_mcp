"""Optional extension ontologies live in a separate graph."""

from pathlib import Path

import pytest

from x3d_utils.ontology import X3DOntology, EXTENSIONS_ENV

FIXTURE = Path(__file__).parent / "fixtures" / "ontology_ext"

JOIN = """
SELECT ?concept WHERE {
  ?concept sc:x3dNode ?node .
  ?node rdfs:subClassOf* x3d:X3DNode .
}
"""


@pytest.fixture(scope="module")
def plain():
    return X3DOntology(extensions="")


@pytest.fixture(scope="module")
def ext():
    return X3DOntology(extensions=str(FIXTURE))


def test_default_graph_is_untouched(plain, ext):
    assert len(plain.graph) == len(ext.graph)
    assert len(plain.extensions) == 0
    assert len(ext.extensions) > 0


def test_extensions_invisible_unless_requested(ext):
    assert ext.query(JOIN) == []
    assert ext.query(JOIN, include_extensions=True) != []


def test_include_extensions_without_any_is_noop(plain):
    assert plain.query(JOIN, include_extensions=True) == []


def test_41_namespace_is_aliased_so_joins_resolve(ext):
    rows = ext.query(JOIN, include_extensions=True)
    assert {r["concept"].split("#")[-1] for r in rows} == {"EyeHeight"}


def test_unresolved_terms_are_reported(ext):
    status = ext.extension_status()
    assert "NoSuchNodeInFourZero" in status["unresolved_x3d_terms"]
    assert "NavigationInfo" not in status["unresolved_x3d_terms"]
    assert status["triples"] == len(ext.extensions)


def test_env_var_is_read(monkeypatch):
    monkeypatch.setenv(EXTENSIONS_ENV, str(FIXTURE / "mini_spatial.ttl"))
    assert len(X3DOntology().extensions) > 0


def test_missing_path_is_an_error():
    with pytest.raises(FileNotFoundError):
        X3DOntology(extensions="/no/such/ontology")
