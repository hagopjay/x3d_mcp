# OrientationInterpolator steps and the shortest path

One semantic check, `check_orientation_steps` in `src/validation/data_checks.py`, with three names.
It reads only the file. Nothing is rendered.

## The fact behind it

An OrientationInterpolator turns from one key value to the next along the shortest path
(ISO/IEC 19775-1, 19.4.6). A step is never more than a half turn. Three things follow:

1. A step **written** as more than pi **plays** as the rest of the circle, the other way round.
2. A step written as a whole turn (0 to 6.28319) is two spellings of one orientation. Nothing turns.
3. A step of exactly a half turn has no shortest path. Which way it goes is decided by how pi was rounded.

Point 3 is the one people and programs fall into, because the usual turntable is written "0, pi, 2 pi":

| Angles in keyValue (axis 0 1 0) | First half | Second half | What you see |
|---|---|---|---|
| `0 3.14159 6.28318` | forward | forward | a full turn, as meant |
| `0 3.1416 6.2832` | back | back | a full turn, the wrong way round |
| `0 3.141593 6.283185` | back | forward | rocks: half a turn back, then returns |
| `0 3.14159265 6.28318531` | forward | back | rocks: half a turn forward, then returns |
| `0 3.1415927 6.2831853` | back | forward | rocks |
| `0 6.28319` | | | nothing moves |
| `0 4` | | | 2.28 rad the other way |
| `0 2.0944 4.18879 6.28319` | forward | forward | a full turn, as meant |

All eight rows were played in X_ITE 16.8.1 (headless Chromium 158, software GL), reading the Transform's
rotation at an eighth, three eighths, five eighths and seven eighths of the cycle. All eight did what the
check predicts from the numbers alone. Script and readings: x3d.loom `flywheel/rules-from-findings/`.

`3.14159265` is 3.6e-9 short of pi. An engine that stores rotations as 32-bit floats reads it as
3.14159274, which is past pi, and turns the other way. X_ITE computes in 64 bits. That step is graded
as engine-dependent, not confirmed, because only one engine was run.

## What the check reports

| Check | Level | When |
|---|---|---|
| `orientation-step-long-way` | warning | A step between two key values on one axis is written as more than pi, and the way it plays does not agree with a neighbouring step. The message gives both angles. |
| `orientation-never-turns` | warning | A step is written as more than pi, yet all key values are one orientation to within 0.02 rad (about one degree). |
| `orientation-half-turn-step` | warning | A step is within 1e-6 rad of pi: 32-bit and 64-bit engines can disagree on the direction. |
| `orientation-half-turn-step` | info | A step is within 1e-3 rad of pi and plays as written. Rounding the angle to fewer digits reverses it. |

The fix is the same each time: add key values so every step is clearly shorter than pi. A full turn
needs at least three steps, for example angles 0, 2.0944, 4.18879, 6.28319.

## What it leaves alone, on purpose

A step over pi is not always a mistake. Baked animations wrap their angles: 6.28 back to 0.01, or pi to
minus three quarters of pi, or an axis that flips sign as the angle passes pi. The author means the short
way and gets it. Such a step is left alone when the way it plays goes the same way round as a neighbouring
step that plays as written. A hold (two identical key values) is never reported.

The price: a spin written as one whole-turn step followed by real motion (`0, 6.28, 7.28`) is not
reported, because it looks like a wrap. The check prefers silence to a false alarm there.

## Survey on real files (26 Nov 2026)

Every distinct `.x3d` file in the project's repositories, each at the head of its default branch
(2,587 by text hash; `node_modules` left out). 463 hold an OrientationInterpolator.

| | Scenes |
|---|---|
| With a warning | 41 of 463 (9%) |
| `orientation-step-long-way` | 16 |
| `orientation-half-turn-step` (warning) | 7 |
| `orientation-never-turns` | 25 |
| Note only (plays as written, fragile) | 31 |

By source: 29 of 190 scenes written by language models in earlier experiments (25 of them spins that
never turn), 4 hand-written game levels (fans turning the wrong way), 4 files of a Blender add-on (two
deliberate probes, two gallery demos), 2 simulation worlds, 1 archived turntable world and 1 older example.

**Correction, same day.** The first run of this survey reported 136 of 443. It read a checkout of x3d.accrete that was one commit behind: 97 of those were turntables the generator fix (x3d.accrete #1, made from failure mode 361) had already regenerated that morning. The numbers here are from every repository at the head of its default branch.

Before the wrap rule above was
added the check also reported 10 scenes with baked animation (sampled paths, a camera track, a crank).
Eight are plain wrapped angles that play as meant. Two are a rolling ball with one step of 10.5 rad among
smaller ones, where the intent cannot be read from the file; they are left silent as well. The 16 warnings other than the 25 still spins were read one by one and are real.

Limits: these are one project's files, and one family makes up most of the count. It is a count of
scenes that play differently from how they are written, not a rate for X3D at large.

Tests: `tests/test_orientation_steps.py` (14). Origin: x3d.loom failure modes 357, 361 and 451.
