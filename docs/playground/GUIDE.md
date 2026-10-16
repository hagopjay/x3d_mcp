# The Accountability Playground: a guide

> X3D is the 3D format an AI can be held accountable to.
>
> Valid is not the same as visible, and X3D is the only format that gives an AI both tests.

You describe a scene. A model writes it as X3D. The validator from this
repository says exactly what is wrong, line by line. The model repairs from
that report. Your own browser renders the result and measures whether
anything is on screen. Off to the side, a second model from a different family
reads the first draft with no validator, and the page shows how its verdict
compares.

## Getting in

The playground is private. There are two ways in:

- **A pass link** from the owner, of the form `…/p/<id>.<signature>`. Open it
  once; it signs you in for 12 hours. A pass can expire or be revoked.
- **Sign in with Google**, if the owner has turned it on and your address or
  your organisation's domain is on the list (for example a whole university
  domain).

If you are told your access has ended, the owner has revoked your pass or
changed the list. Ask them.

## Three ways to play

| Tab | What you do | Does it call a model? |
|---|---|---|
| **Prompt** | Describe a scene in up to 1,200 characters, pick a writer, press **Run the loop** (or Ctrl+Enter) | Yes |
| **Paste X3D** | Paste any X3D (XML) document and press **Validate only**, or let a writer repair it | Validate only: no. Repair: yes |
| **Break it** | Inject one known defect into the current scene and see which test catches it | No |

### The writers

Writers are named by letter (A to F) with a short description. They are
different models; the page never shows which. Each run's judge always comes
from a different family than its writer. A writer marked **off** has been
switched off by the owner.

### The options

- **Model repairs (0 to 3):** how many times the writer may repair from the
  validator's report.
- **X3D version (4.0 or 4.1):** the version the writer must declare. The
  validator checks against that version's schema.
- **The validator applies its own exact fixes first:** some defects (a missing
  profile, an undeclared component) have exactly one correct fix. The validator
  makes those itself, for free, before asking the model.
- **Judge alone:** turn the side-by-side judge on or off.
- **Close the loop:** if the final scene is valid but your browser sees nothing,
  ask the writer for one repair, showing it what the browser saw.

## Reading a run

**The loop, live.** Five stations light up as the run moves: the writer
writes, the validator checks, the validator fixes, the writer repairs, the
browser renders. Teal means the validator acted; blue means a model acted.

**Every step.** One card per draft, with who made it (a writer, the validator,
or you), the full scene, a red marker on every line the validator flagged,
what changed since the last draft, and the defect table:

| Layer | What it checks |
|---|---|
| XML | The file is well-formed: tags close, quotes match |
| XSD | Every element and attribute is allowed by the X3D schema for the declared version |
| semantic | Things a schema cannot see: ROUTEs between real fields of matching types, DEF and USE names, nodes inside the declared profile and components, interpolator key counts |

Each defect has a line, a check name, the exact message and a plain-language
hint. A line marked `≈` was located by the page from the message; the
semantic checks do not report lines themselves.

**The judge panel.** What the judge said about draft 1, set beside the
validator: whether it agreed on valid or invalid, how many of the validator's
defects it found, how many it missed, and how many claims it made that the
validator does not make. Matching is by line (within one) or by a token the
validator quotes, and is labelled as such.

**What your browser sees.** The scene, rendered by X_ITE inside a sealed frame
on your own machine. The page waits for the scene to settle, then counts the
pixels that differ from the background:

| Verdict | Share of the frame |
|---|---|
| BLANK | under 0.05% |
| SPARSE | under 1% (present, but tiny or out of view) |
| VISIBLE | 1% or more |

**The two tiles.** VALID comes from the validator. VISIBLE comes from your
browser. A scene can be one without the other, and the page shows both.

**The receipt.** Run id, verdicts, the scene's SHA-256, the validator commit,
the X_ITE version and your browser, writer and judge, steps, tokens, estimated
cost and time. **Copy receipt** puts it on your clipboard; **Download .x3d** and
**Download report** save the scene and the full report.

## Break it

Each button injects one defect into the current scene:

| Edge colour | Meaning |
|---|---|
| plain | The validator should catch it (a typo in a ROUTE field, a duplicate DEF, a wrong version, broken XML, …) |
| amber | Legal X3D that the validator warns about (a Shape with no geometry) |
| green | Stays valid; only the browser can tell (everything transparent, the camera turned away) |

The score under the buttons counts what each test caught this session.

## Limits

To keep the playground fair and its budget safe, each person has a number of
model runs per hour and per day, there is one run at a time per person, and
the whole playground has daily and per-phase spend caps. Validation without a
model has its own, larger limit. When you hit one, the page says which and when
it resets. The owner can pause model runs entirely; validation still works.

## What is kept

Every run is logged for the owner: the prompt, each step's verdict, the judge's
comparison, your browser's verdict and the final scene. You are logged as a
keyed hash in the run rows, not by your address. Your recent runs appear under
**Your recent runs**; no one else can open them except the owner.

## Glossary

| Term | Meaning |
|---|---|
| X3D | The ISO standard for interactive 3D on the web (ISO/IEC 19775) |
| XSD | The XML schema for one X3D version; it says which elements and attributes are allowed |
| profile, component | The X3D header's promise about which nodes a scene uses; browsers may refuse a scene that breaks it |
| ROUTE | A wire from one node's output field to another node's input field |
| DEF / USE | A name for a node, and a reuse of that named node |
| X_ITE | The open-source X3D browser that renders every scene here |
| judge | A model asked to review a scene with no validator, to show what a model alone catches |
