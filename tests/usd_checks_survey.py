"""Run the USD-answer checks (src/validation/usd_checks.py) over real X3D files and count files per finding.

    python tests/usd_checks_survey.py DIR [DIR ...] [--json out.json] [--examples N]

Fixtures cannot find false alarms; real files can (x3d.loom failure mode 324). This is the survey that was
run before these checks shipped; its numbers are in docs/USD-VALIDATION-PARITY.md's companion notes.
"""
from __future__ import annotations

import json
import sys
from collections import defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
from lxml import etree  # noqa: E402

from validation import usd_checks  # noqa: E402
from validation.semantic import _PARSER, _find_scene  # noqa: E402


def main(argv):
    dirs = [a for a in argv if not a.startswith("--") and not a[0].isdigit()]
    out = argv[argv.index("--json") + 1] if "--json" in argv else None
    n_ex = int(argv[argv.index("--examples") + 1]) if "--examples" in argv else 3
    files = sorted({p.resolve() for d in dirs for p in Path(d).rglob("*.x3d") if "node_modules" not in p.parts})
    by_check, examples, parsed, failed = defaultdict(set), defaultdict(list), 0, 0
    for f in files:
        try:
            root = etree.fromstring(f.read_bytes(), _PARSER)
            scene = _find_scene(root)
            if scene is None:
                continue
        except Exception:
            failed += 1
            continue
        parsed += 1
        for fn in usd_checks.USD_SCENE_CHECKS:
            try:
                ds = fn(scene)
            except Exception as e:                         # a crash on a real file is a finding too
                by_check["CRASH:" + fn.__name__].add(str(f))
                examples["CRASH:" + fn.__name__].append(f"{f}: {e!r}")
                continue
            for d in ds:
                key = f"{d.check} ({d.level})"
                if str(f) not in by_check[key] and len(examples[key]) < n_ex:
                    examples[key].append(f"{f}: {d.message[:220]}")
                by_check[key].add(str(f))
    rows = sorted(((k, len(v)) for k, v in by_check.items()), key=lambda kv: -kv[1])
    print(f"{len(files)} files, {parsed} parsed, {failed} not parsed")
    for k, n in rows:
        print(f"{n:5d}  {k}")
    if out:
        Path(out).write_text(json.dumps({"files": len(files), "parsed": parsed, "counts": dict(rows),
                                         "examples": examples}, indent=1))


if __name__ == "__main__":
    main(sys.argv[1:])
