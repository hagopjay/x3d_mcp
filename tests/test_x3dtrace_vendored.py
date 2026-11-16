"""The vendored x3dtrace engine against the conformance rows it was written to.

`src/x3d_utils/x3dtrace.py` is a copy of x3d.ML's `src/x3dtrace.py` (see NOTICE-x3dtrace.md for the commit and the sha256 this copy
must keep). `tests/fixtures/trace_conformance.json` is that repository's file; its rows carry what GAIK (MIT) does and what x3dtrace
should do, with a reason wherever they differ. A copy that has drifted from the engine fails here before it fails in a client.
"""

import hashlib
import json
import re
import sys
from decimal import Decimal
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from x3d_utils import x3dtrace as T

FX = json.loads((Path(__file__).resolve().parent / "fixtures" / "trace_conformance.json").read_text(encoding="utf-8"))


def decode(v):
    if isinstance(v, dict) and "$decimal" in v:
        return Decimal(v["$decimal"])
    if isinstance(v, dict) and "$float" in v:
        return float(v["$float"])
    return v


def test_engine_copy_matches_the_recorded_sha256():
    notice = (ROOT / "NOTICE-x3dtrace.md").read_text(encoding="utf-8")
    m = re.search(r"x3dtrace\.py sha256 ([0-9a-f]{64})", notice)
    assert m, "NOTICE-x3dtrace.md must record the sha256 of the vendored engine"
    assert hashlib.sha256((ROOT / "src" / "x3d_utils" / "x3dtrace.py").read_bytes()).hexdigest() == m.group(1)


@pytest.mark.parametrize("r", [pytest.param(r, id=r["id"]) for r in FX["value_status"]])
def test_value_status_rows(r):
    got = T.compare(decode(r["value"]), r["quote"], T.lex(r["quote"], r["mode"]), T.TracePolicy(**r["policy"]), r["sem"]).state
    assert got == r["x3dtrace_expected"], (r["id"], r["reason"])


@pytest.mark.parametrize("r", [pytest.param(r, id="%s-%s" % (r["mode"], i)) for i, r in enumerate(FX["tokens"])])
def test_token_rows(r):
    assert T.lex(r["text"], r["mode"]).norms == r["x3dtrace_expected"], r["reason"]


@pytest.mark.parametrize("r", [pytest.param(r, id=repr(r["text"])[:30]) for r in FX["number_readings"]])
def test_number_reading_rows(r):
    q = T.lex(r["text"], "prose")
    vals = sorted({round(n.value, 12) for n in T.quote_numbers(q, r["text"], T.TracePolicy()) if not n.word})
    assert vals == [round(v, 12) for v in r["x3dtrace_expected"]], r["reason"]


@pytest.mark.parametrize("r", [pytest.param(r, id="v%d-%s-%s" % (i, r["status"], r["value_status"])) for i, r in enumerate(FX["verdict_of"])])
def test_verdict_rows(r):
    pol = T.TracePolicy(**r["policy"])
    got = T.verdict_of(r["status"], r["value_status"], r["value"], len(T.lex(r["quote"], "prose").norms), pol, r["occurrences"])
    assert got == r["x3dtrace_expected"], r["reason"]
