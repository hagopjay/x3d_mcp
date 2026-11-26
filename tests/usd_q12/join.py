"""Stage 3 of Q12: join the X3D verdicts with the USD rulebook verdicts on the same scenes, per rule.

    python tests/usd_q12/join.py SCENES_DIR EXPORT.json RULEBOOKS.json OUT.json [--blender-addon-commit SHA]

Scenes are the parity fixtures (tests/usd_parity.py F: a failing and a passing scene per stateable mistake), written
to SCENES_DIR by `dump_scenes.py`. For each USD rule that was actually executed and that tests/usd_parity.py MAP
says X3D can state (with a check), every scene gets one class:

    agree-fail     the X3D check fires and the USD rule fails
    agree-pass     both are quiet
    x3d-only       the X3D check fires, the USD rule passes: Blender repaired it, or our check is stricter than the rule
    export-made    the X3D check is quiet and the whole scene has no X3D finding, yet the USD rule fails: a loss or a
                   convention of the Blender leg. Goes to EXPECTED_LOSS or the add-on, never against x3d_mcp
    x3d-missed?    the X3D check is quiet, the USD rule fails, and the scene has an X3D finding for another reason:
                   a candidate x3d_mcp defect, to be looked at by a person (it may still be export-made)

USD rules that were executed, that MAP does not tie to an X3D check, and that fail on some exported scene are listed
under `unmapped_failures`: they are Blender-leg behaviour (export-made) by construction.
"""
import json, subprocess, sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import usd_parity as u  # noqa: E402
import gltf_parity as gp  # noqa: E402

STATEABLE = u.STATEABLE


def x3d_view(text):
    ok, xsd, ds = gp.findings(text, None)
    codes = sorted({c for lv, c in ds if lv in ("error", "warning")})
    info = sorted({c for lv, c in ds if lv == "info"})
    return {"parse_ok": ok, "xsd": xsd, "codes": codes, "info": info}


def fires(view, check, by=""):
    """As tests/usd_parity.py run_fixture: info-level checks count when the fixture says so."""
    if check is None:
        return view["xsd"] > 0 or not view["parse_ok"]
    return check in view["codes"] or (by.endswith("-info") and check in view["info"])


def main(scenes, exp, rb, dest, addon_commit=""):
    exp, rb = json.loads(Path(exp).read_text()), json.loads(Path(rb).read_text())
    texts = {p.stem: p.read_text() for p in Path(scenes).glob("*.x3d")}
    views = {n: x3d_view(t) for n, t in texts.items()}
    usd = rb["scenes"]
    executed = {r for s in usd.values() for r in s}
    out_rows, tally = [], {}
    mapped = {rid: (st, fx) for rid, (st, fx, _) in u.MAP.items() if st in STATEABLE and rid in executed}
    for rid, (st, fx) in sorted(mapped.items()):
        check, by = (u.F[fx][1], u.F[fx][0]) if fx else (None, "")
        own = {f"{fx}__bad", f"{fx}__good"}
        for n, v in views.items():
            if check is None and n not in own:
                continue  # a schema rule is compared only on its own fixture pair: other scenes break other schema rules
            if n not in usd:
                cls = "export-failed"
            else:
                r = usd[n].get(rid, "")
                if r.startswith("not-run"):
                    cls = "not-run"
                else:
                    uf, xf = r == "fail", fires(v, check, by)
                    flagged = bool(v["codes"]) or v["xsd"] > 0 or not v["parse_ok"]
                    cls = (("agree-fail" if uf else "agree-pass") if uf == xf else "x3d-only" if xf else ("x3d-missed?" if flagged else "export-made"))
            tally.setdefault(rid, {}).setdefault(cls, 0)
            tally[rid][cls] += 1
            if cls not in ("agree-pass", "agree-fail"):
                out_rows.append({"rule": rid, "scene": n, "class": cls, "x3d_codes": v["codes"], "usd": usd.get(n, {}).get(rid)})
    unmapped = {}
    for n, s in usd.items():
        for rid, r in s.items():
            if rid not in mapped and r == "fail":
                unmapped.setdefault(rid, []).append(n)
    try:
        commit = subprocess.run(["git", "-C", str(HERE), "rev-parse", "--short", "HEAD"], capture_output=True, text=True).stdout.strip()
    except Exception:
        commit = ""
    totals = {}
    for t in tally.values():
        for k, c in t.items():
            totals[k] = totals.get(k, 0) + c
    scenes_ok = sum(1 for n in views if n in usd and "error" not in exp["scenes"].get(n, {}))
    agree_scenes = sum(1 for n in views if n in usd and all(
        usd[n].get(rid, "").startswith("not-run") or ((usd[n].get(rid) == "fail") == fires(views[n], u.F[fx][1] if fx else None, u.F[fx][0] if fx else ""))
        for rid, (st, fx) in mapped.items() if fx and (u.F[fx][1] is not None or n.split("__")[0] == fx)))
    receipt = {
        "pins": {**u.RULES["pins"], "blender": exp["blender"], "rulebook_runner": rb["versions"],
                 "x3d_mcp_commit": commit, "x3d_blender_io_commit": addon_commit},
        "scenes": len(views), "exported_ok": scenes_ok, "executed_rules_mapped_to_x3d": len(mapped),
        "executed_rules_total": len(executed), "pair_totals": totals,
        "scenes_agreeing_on_every_mapped_rule": agree_scenes,
        "per_rule": tally, "unmapped_failures": unmapped, "disagreements": out_rows,
    }
    Path(dest).write_text(json.dumps(receipt, indent=1))
    print(f"{scenes_ok} of {len(views)} scenes exported; {len(mapped)} mapped rules executed; pairs {totals}")
    print(f"scenes agreeing on every mapped rule: {agree_scenes} of {len(views)}")
    print("unmapped failures:", {k: len(v) for k, v in unmapped.items()})


if __name__ == "__main__":
    a = sys.argv[1:]
    ac = a[a.index("--blender-addon-commit") + 1] if "--blender-addon-commit" in a else ""
    main(a[0], a[1], a[2], a[3], ac)
