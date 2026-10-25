"""validation.splat_arrays.check_splat_arrays must report exactly what validation.semantic._check_gaussian_splats reports."""
import random
import sys
from pathlib import Path

import numpy as np
from lxml import etree

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

from validation.splat_arrays import SPLAT_SH, check_splat_arrays
from validation import semantic


def old(attrs):
    el = etree.Element("GaussianSplats", **{k: v for k, v in attrs.items() if v is not None})
    scene = etree.Element("Scene"); scene.append(el)
    return [(d.level, d.check, d.message.replace("GaussianSplats: ", "", 1)) for d in semantic._check_gaussian_splats(scene)]


def new(attrs):
    return [(f.level, f.check, f.message) for f in check_splat_arrays(attrs)]


def _nums(rng, k, lo=-1.0, hi=1.0, sep=" "):
    return sep.join(f"{rng.uniform(lo, hi):.4f}" for _ in range(k))


def random_attrs(rng):
    n = rng.choice([1, 2, 5, 50, 130, 300])
    sep = rng.choice([" ", ", ", "\n", " ,  "])
    a = {}
    def maybe(name, k, **kw):
        if rng.random() < 0.9:
            kk = n * k + rng.choice([0, 0, 0, 0, 1, -1, k]) if rng.random() < 0.25 else n * k
            a[name] = _nums(rng, max(kk, 0), sep=sep, **kw)
    maybe("positions", 3)
    maybe("scales", 3, lo=-0.5 if rng.random() < 0.2 else 0.01, hi=0.5)
    maybe("opacities", 1, lo=-3 if rng.random() < 0.2 else 0, hi=1 if rng.random() < 0.8 else 4)
    mode = rng.random()
    if mode < 0.3:   # w x y z, unit
        q = np.array([[rng.uniform(0.9, 1)] + [rng.uniform(-0.1, 0.1) for _ in range(3)] for _ in range(n)])
    else:            # x y z w
        q = np.array([[rng.uniform(-0.1, 0.1) for _ in range(3)] + [rng.uniform(0.9, 1)] for _ in range(n)])
    q /= np.linalg.norm(q, axis=1, keepdims=True) * (1.0 if rng.random() < 0.7 else rng.uniform(0.9, 1.1))
    if rng.random() < 0.1:
        q[0] = 0
    a["orientations"] = sep.join(f"{v:.5f}" for v in q.ravel())
    if rng.random() < 0.9:
        a["sphericalHarmonicsDegree0Coef0"] = _nums(rng, n * 3, sep=sep)
    for d in range(1, 4):
        if rng.random() < 0.3:
            for c in range(2 * d + 1):
                if rng.random() < 0.9:
                    a[SPLAT_SH[(d, c)]] = _nums(rng, n * 3, sep=sep)
    return a


def test_random_attribute_sets_agree():
    rng = random.Random(7)
    for _ in range(400):
        attrs = random_attrs(rng)
        assert new(attrs) == old(attrs), attrs.keys()


def test_empty_and_odd_values_agree():
    for attrs in ({}, {"positions": ""}, {"positions": "  "}, {"positions": "1 2", "orientations": "1"}, {"opacities": "0.5", "scales": "-1"},
                  {"positions": "0 0 0", "orientations": "0 0 0 0"}):
        assert new(attrs) == old(attrs)


def test_generator_reaches_every_check():
    rng = random.Random(7)
    seen = set()
    for _ in range(400):
        seen |= {c for _, c, _ in new(random_attrs(rng))}
    assert seen >= {"tuple-count", "splat-array-length", "splat-sh-partial", "splat-no-color", "splat-scale-space",
                    "splat-opacity-space", "splat-quaternion-norm", "splat-quaternion-order"}, seen


def test_without_numpy_counts_still_checked(monkeypatch):
    import sys
    monkeypatch.setitem(sys.modules, "numpy", None)
    out = check_splat_arrays({"positions": "0 0 0 1 1 1", "opacities": "0.5", "scales": "-1 -1 -1 1 1 1"})
    assert [f.check for f in out] == ["splat-array-length", "splat-no-color", "splat-values-unchecked"]


def _numpy1_fromstring(monkeypatch):
    """numpy 1.26 np.fromstring: a bad token only warns and the numbers before it are returned."""
    import warnings
    real = np.fromstring

    def old_behaviour(text, dtype=float, sep=""):
        out = []
        for tok in text.split():
            try:
                out.append(float(tok))
            except ValueError:
                warnings.warn("string or file could not be read to its end due to unmatched data", DeprecationWarning)
                break
        return np.array(out, dtype=dtype)
    monkeypatch.setattr(np, "fromstring", old_behaviour)
    return real


def test_a_bad_token_is_found_where_numpy_only_warns(monkeypatch):
    from validation.splat_arrays import first_bad_token, parse_numbers, ArrayError
    import pytest
    _numpy1_fromstring(monkeypatch)
    attrs = {"positions": "0 0 0 1 1 1 x", "scales": "1 1 1 1 1 1", "orientations": "0 0 0 1 0 0 0 1", "opacities": "1 1",
             "sphericalHarmonicsDegree0Coef0": "0 0 0 0 0 0"}
    found = check_splat_arrays(attrs, strict_numbers=True)
    assert [f.check for f in found] == ["splat-array-syntax", "tuple-count"] and "'x'" in found[0].message
    with pytest.raises(ArrayError, match="not a number: 'x'"):
        parse_numbers("1 2 x", strict=True)
    assert parse_numbers("1 2 x").size == 0
    assert first_bad_token("1 2 , 3 4 y 5") == "y" and first_bad_token("1 2") is None
