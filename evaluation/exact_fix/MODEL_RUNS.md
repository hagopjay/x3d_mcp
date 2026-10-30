# Exact-fix hints with real models (2026-10-29)

`evaluation/hint_loop_gemini.py --go`, Gemini API (shared ledger in x3d.onto `xdomain/eval-plan/gemini_api.py`, branch
`loom/gemini-api`), temperature 1.0, 2 rounds, corpus from `exact_fix_eval.py` (seed 20261029, 540 cases, 12 classes of 45).
Arms: **E** = the validator's errors only, **H** = the same errors each followed by its exact-fix hint. Paired per case;
"valid" = XSD plus error-level semantic checks. Grades: **C** measured here, **L** my reading.

| model | cases | E valid r1 | H valid r1 | H vs E r1 (gained / lost, McNemar p) | E valid r2 | H valid r2 | H vs E r2 | est. $ |
|---|---:|---:|---:|---|---:|---:|---|---:|
| gemini-3.8-flash | 60 | 59 | 57 | 0 / 2, p 0.5 | 60 | 60 | 0 / 0 | 1.11 |
| gemini-3.1-flash-lite | 60 | 60 | 59 | 0 / 1, p 1.0 | 60 | 60 | 0 / 0 | 0.84 |
| gemini-2.5-flash-lite | 60 | 53 | 55 | 3 / 1, p 0.63 | 57 | 58 | 1 / 0 | 0.88 |
| **gemini-2.5-flash-lite** | **540** | **489 (90.6%)** | **507 (93.9%)** | **31 / 13, p 0.0096** | 524 | 530 | 11 / 5, p 0.21 | 7.33 |

All C. One of the two 3.8-flash H losses is a connection reset in round 1, not a model answer (C); `gemini_api.py` now retries
transient errors, and the later runs had no call errors. Dollar figures are the ledger's upper-bound estimate (every
non-pro model priced as flash, $0.75 / $3.75 per 1M tokens, thinking billed as output), so Flash-Lite is overstated.

Where the 540-case gain comes from (round 1, E → H, of 45 each; C):

| class | E r1 | H r1 | | class | E r1 | H r1 |
|---|---:|---:|---|---|---:|---:|
| misplaced-route | 32 | **45** | | wrong-parent | 43 | 41 |
| random-relocation | 35 | 39 | | missing-required | 41 | 40 |
| unknown-attribute | 42 | 44 | | two-faults | 29 | 29 |
| colour | 44 | 45 | | unknown-node | 44 | 44 |
| wrong-field | 44 | 45 | | bad-enum, bad-value, route-field | 45 | 45 |

Reading (L):
- Hints help when the model is weak enough to miss on the first repair, and the gain is concentrated where the plain error
  does not say what to do: a ROUTE placed before its nodes (32 → 45) and a node relocated to a wrong field. Where the error
  text already names the fix (bad enum, bad value, route field) both arms are at 45 of 45.
- By round 2 plain errors mostly catch up (524 vs 530, not significant): the hint buys one round, not a capability.
- On 3.8-flash and 3.1-flash-lite the corpus is too easy (59-60 of 60 without hints), so these runs cannot show a gain there.
  On 3.8-flash the hint arm still used 17% fewer output tokens (116k vs 141k) and was 22% faster per first answer
  (11.7 s vs 15.0 s mean) (C, one run).
- two-faults stays at 29 of 45 in round 1 in both arms: one round fixes one fault; the second round reaches 44 in both.

Logs (gitignored here): `evaluation/exact_fix/model_runs/`; archived copies in the project's
`x3d-loom/runs/exact-fix/`.
