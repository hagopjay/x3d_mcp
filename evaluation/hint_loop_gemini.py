"""Model run, not yet done: does a repair prompt with exact-fix hints beat one with plain errors?

Paired design on the broken-scene corpus that evaluation/exact_fix_eval.py writes
(evaluation/exact_fix/corpus.jsonl). Each case goes to the model twice:

  E  the validator's errors only (the report the eval-plan runner sends today)
  H  the same errors, each followed by its exact-fix hint ("Fix: ..." plus the
     suggested_fix as JSON)

for up to --rounds rounds; after each answer the scene is re-checked with the
same gate (XSD plus error-level semantic checks). Reported: valid after round 1
and after the last round per arm, paired gains and losses with an exact McNemar
p, and how often the answer kept the scene (Shape count not lower than the
broken scene's).

Gemini calls go through the shared ledger when it is importable: gemini_api.py
(call(), spent()) from the x3d.onto checkout, looked up in $X3D_ONTO_EVAL_PLAN,
then ~/x3d.onto/xdomain/eval-plan and /home/claude/x3d.onto/xdomain/eval-plan
(branch loom/gemini-api). Otherwise a small REST client calls the Gemini API
with the key in GEMINI_API_KEY, else GOOGLE_API_KEY. The playground and the
eval-plan runner use Vertex with a service identity and have no API key, so
this script does not reuse them.

    uv run python evaluation/hint_loop_gemini.py --dry-run            # free: a stub model, checks the plumbing
    uv run python evaluation/hint_loop_gemini.py --go --limit 60 --max-calls 300

--go is required for real calls; --max-calls caps them.
"""

import argparse
import collections
import importlib.util
import json
import math
import os
import random
import re
import ssl
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from validation.hints import apply_fix, gate  # noqa: E402

CORPUS = ROOT / "evaluation" / "exact_fix" / "corpus.jsonl"
RUNS = ROOT / "evaluation" / "exact_fix" / "model_runs"
DEFAULT_MODEL = "gemini-3.8-flash"          # Writer A in playground/config.py
LEDGER_DIRS = [os.environ.get("X3D_ONTO_EVAL_PLAN", ""), "~/x3d.onto/xdomain/eval-plan",
               "/home/claude/x3d.onto/xdomain/eval-plan"]


# ---------------------------------------------------------------------------
# prompts

def report(errors: list[str], hints: list[dict], with_hints: bool, cap: int = 8) -> str:
    by = collections.defaultdict(list)
    for h in hints:
        if "error_index" in h:
            by[h["error_index"]].append(h)
    lines = []
    for i, e in enumerate(errors[:cap]):
        line = f"- {e}"
        if with_hints:
            for h in by.get(i, [])[:1]:
                line += f"\n  Fix: {h.get('message', '')}"
                if h.get("suggested_fix"):
                    line += f"\n  Exact edit: {json.dumps(h['suggested_fix'], separators=(',', ':'))}"
        lines.append(line)
    if len(errors) > cap:
        lines.append(f"(+{len(errors) - cap} more)")
    return "\n".join(lines)


def prompt(doc: str, errors: list[str], hints: list[dict], with_hints: bool) -> str:
    return ("This X3D scene (XML encoding) fails validation.\n\n"
            f"```xml\n{doc}\n```\n\nThe validator reported:\n{report(errors, hints, with_hints)}\n\n"
            "Return the complete corrected scene only, in one code block. Keep everything else as it is.")


def extract(text: str) -> str:
    blocks = re.findall(r"```[a-zA-Z0-9]*\n(.*?)```", text, re.S)
    return max(blocks, key=len).strip() if blocks else text.strip()


# ---------------------------------------------------------------------------
# models

def _ledger_module():
    for d in LEDGER_DIRS:
        p = Path(d).expanduser() / "gemini_api.py" if d else None
        if p and p.exists():
            spec = importlib.util.spec_from_file_location("gemini_api", p)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod, p
    return None, None


def _rest_call(model: str, text: str, max_tokens: int, temperature: float) -> str:
    key = os.environ.get("GEMINI_API_KEY") or os.environ.get("GOOGLE_API_KEY")
    if not key:
        raise SystemExit("no GEMINI_API_KEY or GOOGLE_API_KEY in the environment")
    url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"
    body = {"contents": [{"role": "user", "parts": [{"text": text}]}],
            "generationConfig": {"maxOutputTokens": max_tokens, "temperature": temperature}}
    req = urllib.request.Request(url, data=json.dumps(body).encode(), method="POST",
                                 headers={"Content-Type": "application/json", "x-goog-api-key": key})
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    ctx = ssl.create_default_context(cafile=bundle) if bundle else None
    for attempt in range(6):
        try:
            with urllib.request.urlopen(req, timeout=600, context=ctx) as r:
                data = json.loads(r.read())
            parts = data.get("candidates", [{}])[0].get("content", {}).get("parts", [])
            return "".join(p.get("text", "") for p in parts)
        except urllib.error.HTTPError as e:
            if e.code not in (429, 500, 503) or attempt == 5:
                raise
            time.sleep(min(120, 5 * 2 ** attempt))
    return ""


def real_model(model: str, max_tokens: int, temperature: float):
    mod, where = _ledger_module()
    if mod is not None:
        print(f"Gemini calls through the shared ledger module {where}")

        def f(text, purpose=""):
            r = mod.call(model, [text], max_tokens=max_tokens, temperature=temperature,
                         purpose=f"exact-fix hint loop {purpose}")
            return r["text"] if isinstance(r, dict) else str(r)
        f.spent = getattr(mod, "spent", lambda: None)
        return f
    print("gemini_api.py not found; calling the Gemini REST API directly (no shared ledger)")

    def g(text, purpose=""):
        return _rest_call(model, text, max_tokens, temperature)
    g.spent = lambda: None
    return g


def stub_model(text, purpose=""):
    """Not a model: under H it applies the first exact edit in the prompt, under E it returns the scene unchanged."""
    doc = extract(text.split("The validator reported:")[0])
    m = re.search(r"Exact edit: (\{.*\})", text)
    if m:
        try:
            doc = apply_fix(doc, json.loads(m.group(1)))
        except Exception:      # noqa: BLE001
            pass
    return f"```xml\n{doc}\n```"


stub_model.spent = lambda: 0.0


# ---------------------------------------------------------------------------

def shapes(doc: str) -> int:
    return len(re.findall(r"<Shape\b", doc))


def mcnemar(b: int, c: int) -> float:
    n = b + c
    if n == 0:
        return 1.0
    k = min(b, c)
    return min(1.0, 2 * sum(math.comb(n, i) for i in range(k + 1)) / 2 ** n)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--limit", type=int, default=60, help="cases, stratified by class")
    ap.add_argument("--rounds", type=int, default=2)
    ap.add_argument("--seed", type=int, default=20261029)
    ap.add_argument("--max-calls", type=int, default=300)
    ap.add_argument("--max-tokens", type=int, default=16384)
    ap.add_argument("--temperature", type=float, default=1.0)
    ap.add_argument("--go", action="store_true", help="allow real (paid) model calls")
    ap.add_argument("--dry-run", action="store_true", help="use the stub model; no network")
    a = ap.parse_args()
    if not CORPUS.exists():
        raise SystemExit(f"{CORPUS} missing: run evaluation/exact_fix_eval.py first")
    cases = [json.loads(l) for l in CORPUS.read_text().splitlines() if l.strip()]
    rng = random.Random(a.seed)
    by = collections.defaultdict(list)
    for c in cases:
        by[c["class"]].append(c)
    for v in by.values():
        rng.shuffle(v)
    picked, i = [], 0
    while len(picked) < min(a.limit, len(cases)):
        for k in sorted(by):
            if i < len(by[k]) and len(picked) < a.limit:
                picked.append(by[k][i])
        i += 1
    if a.dry_run:
        model, name = stub_model, "stub"
    elif a.go:
        model, name = real_model(a.model, a.max_tokens, a.temperature), a.model
    else:
        raise SystemExit("refusing to call a model without --go (or use --dry-run)")
    calls = 0
    RUNS.mkdir(parents=True, exist_ok=True)
    out = RUNS / f"{name}-{time.strftime('%Y%m%dT%H%M%S')}.jsonl"
    rows = []
    with out.open("w") as log:
        for c in picked:
            row = {"id": c["id"], "class": c["class"]}
            for arm, with_hints in (("E", False), ("H", True)):
                doc, state, hist = c["broken"], gate(c["broken"]), []
                for r in range(1, a.rounds + 1):
                    if state["valid"]:
                        break
                    if calls >= a.max_calls:
                        raise SystemExit(f"--max-calls {a.max_calls} reached; partial log in {out}")
                    calls += 1
                    t0 = time.time()
                    try:
                        answer = model(prompt(doc, state["errors"], state["hints"], with_hints), f"{c['id']} {arm} r{r}")
                        doc = extract(answer)
                    except SystemExit:
                        raise
                    except Exception as exc:     # noqa: BLE001 - a failed call is a failed round
                        hist.append({"round": r, "error": str(exc)[:200]})
                        continue
                    state = gate(doc)
                    hist.append({"round": r, "valid": state["valid"], "errors": len(state["errors"]),
                                 "seconds": round(time.time() - t0, 1)})
                row[arm] = {"valid_r1": bool(hist and hist[0].get("valid")), "valid": state["valid"],
                            "kept_shapes": shapes(doc) >= shapes(c["broken"]), "rounds": hist}
            rows.append(row)
            log.write(json.dumps(row) + "\n")
            log.flush()
    n = len(rows)
    summ = {"model": name, "cases": n, "calls": calls, "log": str(out.relative_to(ROOT))}
    for arm in ("E", "H"):
        summ[f"{arm}_valid_round1"] = sum(r[arm]["valid_r1"] for r in rows)
        summ[f"{arm}_valid_final"] = sum(r[arm]["valid"] for r in rows)
        summ[f"{arm}_kept_shapes"] = sum(r[arm]["kept_shapes"] for r in rows)
    for key in ("valid_r1", "valid"):
        gain = sum(r["H"][key] and not r["E"][key] for r in rows)
        loss = sum(r["E"][key] and not r["H"][key] for r in rows)
        summ[f"H_vs_E_{key}"] = {"gained": gain, "lost": loss, "mcnemar_p": round(mcnemar(gain, loss), 6)}
    summ["spent"] = model.spent() if hasattr(model, "spent") else None
    (out.with_suffix(".summary.json")).write_text(json.dumps(summ, indent=1) + "\n")
    print(json.dumps(summ, indent=1))


if __name__ == "__main__":
    main()
