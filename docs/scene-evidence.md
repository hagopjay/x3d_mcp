# scene_evidence: is the quote in the source, is the value in the quote, does the scene hold it

`scene_evidence` checks the evidence behind a scene's values. A writer (a model, a tool, a person) lists one claim per value: the field it set, the value, and the **exact words** it read the value from. Plain code, with no model call, answers three questions for each claim:

| Step | Question | Answer words |
|---|---|---|
| FIND | Is the quote in the source it names? | `found`, `not_found`, `moved` (found in another source), `no_quote`, `source_missing` |
| COMPARE | Is the value inside the quote? | `match`, `near`, `mismatch`, `unchecked` |
| BIND | Does the scene hold the claimed value at that field? | `match`, `mismatch`, `no-field`, `no-attribute` |

and gives a verdict: `verified` (quote found, value inside it), `reworded` (the value is a close reading, for example a unit or a number word), `check` (not found, or the value is not in the quote), `unchecked` (no quote). The method is Umair Ali Khan's, from the article "How I Added Traceability in Structured Data Extraction with AI" and his GAIK toolkit (MIT). The engine behind this tool, `x3d_utils/x3dtrace.py`, is an independent implementation; see `NOTICE-x3dtrace.md` for the credit and every deliberate difference, and the x3d.ML repository (`docs/TRACE.md`, `docs/TRACE-REVIEW.md`) for the engine's own guide and the independent review of it.

It reads the same inputs as the validators (`content`, `path`, `blob_id` or `current_scene`), changes nothing, and calls no model. Like `scene_brief`, **it never repeats a sentence a document or the scene's author wrote**: an item carries verdict words, offsets, line and column, and node handles. A quote appears only as a length and an 8-hex digest unless you ask for `text="quoted"`.

## A call

```json
{"content": "<X3D ...>",
 "sources": [{"id": "prompt", "text": "Put a red ball of radius 0.5 m one metre above the floor. Next to it a crate 1 x 2 x 1, turned 90 degrees about y."}],
 "claims": [
  {"field": "n7@radius",      "value": 0.5,            "quote": "radius 0.5 m",              "source": "prompt"},
  {"field": "crate@rotation", "value": "0 1 0 1.5708", "quote": "turned 90 degrees about y", "source": "prompt"},
  {"field": "n10@size",       "value": "1 2 1",        "quote": "a crate 1 x 2 x 1",         "source": "prompt"},
  {"field": "ball@translation","value": "0 2 0",       "quote": "one metre above the floor", "source": "prompt"}],
 "expect": "inventory"}
```

`field` is one of `DEF@attr`, `Tag[DEF].attr`, `n<k>@attr` (the handle `scene_brief` prints for an element) or the engine's XPath form `/X3D/Scene/Transform[@DEF='ball']/@translation`. A Sphere with no `DEF` is reached by its handle (`n7`), not by its tag.

The answer to that call (abridged to the parts a reader needs; the real answer is 2,515 bytes):

```json
{"schema":"x3d-scene-evidence/1","ok":true,"engine":"x3dtrace/0.1","tier":"oracle",
 "summary":{"verified":3,"check":1,"reworded":0,"unchecked":0,"total":4,"bound":3,"unbound":1,
            "coverage":0.571429,"verified_coverage":0.428571,"expected":7},
 "items":[
  {"i":0,"verdict":"verified","value_status":"match","grade_cap":"Confirmed",
   "bind":{"state":"match","handle":"n7","node":"Sphere","attr":"radius","line":4},"span":[18,30]},
  {"i":1,"verdict":"verified","value_status":"match","grade_cap":"Confirmed","derivation":["axis","deg2rad"],
   "bind":{"state":"match","handle":"n8","node":"Transform","attr":"rotation","line":5},"span":[88,113]},
  {"i":3,"verdict":"check","value_status":"mismatch","grade_cap":"Suspected",
   "bind":{"state":"mismatch","handle":"n3","node":"Transform","attr":"translation","line":4},"span":[31,56]}],
 "claims_sha256":"...","report_sha256":"..."}
```

Claim 3 says the ball is two metres up and quotes "one metre above the floor": the quote is found, the value is not in it, and the scene (which holds `0 1 0`) does not hold `0 2 0` either. The tool reports all three facts separately, because a wrong quote and a wrong scene are different repairs. Claim 1 is `verified` because the quote says "90 degrees about y" and the value `0 1 0 1.5708` is that rotation in radians; `derivation` names the two conversions the engine had to apply, and it applies one only when the quote itself carries the evidence for it.

## What the words mean for judgment work

`grade_cap` is the highest grade `claim_append` should be given for that value if the evidence is all there is: `verified` caps at Confirmed, `reworded` or `unchecked` at Likely, `check` at Suspected. The cap is a ceiling. **`verified` means the quote contains the value. It does not mean the quote is the right sentence, or that the value is true**; a repeated quote is resolved only with `prefix`/`suffix` or the field's own node, and `occurrences` says how many times it was found. `bound` is separate: it says the scene holds the value, which a model can get right with no source at all.

`expect` turns on coverage: `""` (none), `"inventory"` (every value-bearing attribute of the scene) or a comma list of fields. `coverage` is the share of expected fields that some claim cites; `verified_coverage` is the share with a verified claim and is the number to trust, because a fabricated quote still counts as cited.

## Inputs

| Argument | Meaning |
|---|---|
| `claims` or `claims_blob_id` | JSON list (or `{"claims": [...]}`) of `{field, value, quote, source?, id?, prefix?, suffix?}`; up to 2,000 claims, quotes up to 2,000 characters |
| `sources` | JSON list of `{id, text}` or `{id, blob_id}`, at most 16, 8 MB of text each. The scene is always source `scene`, so a claim may quote the scene itself |
| `policy` | `default`, `strict` (no `round_to_printed`, reworded is a problem) or a JSON object of `number_tolerance`, `round_to_printed`, `reworded_is_problem`, `min_quote_words`, `ambiguous_quote_is_check`, `moved_is_check`, `code_values_unchecked`, `length_scale`, `match_reformatted_identifiers` |
| `bind` | `true` (default): also check the scene's own value at `field` |
| `text` | `counts` (default), `off`, or `quoted` (quotes, capped to `text_cap` characters, JSON-escaped, under `untrusted`) |
| `max_items` | items returned, 1 to 2,000, default 200; the totals always cover every claim |
| `expect_report_sha256` | a `report_sha256` from an earlier answer; the answer says whether the check still recomputes to it |
| `return_mode` | `full` (default), `summary` (counts and the card) or `blob` (stores the report, returns its id) |

Over HTTP, `path` stays stdio-only and `blob_id` and `claims_blob_id` go through the same bearer gate as the other tools.

## What is kept out

A claim's `value`, a quote, a source's text, a free `id` that is not identifier-shaped and a `field` that is not a plain name never appear in the answer. Node and attribute names come from the vetted X3DUOM names (the same helpers `scene_brief` uses), not from the document. The tests include a hostile scene, a hostile quote and a hostile field, and a bidirectional-control character in a quoted answer (it is escaped).

## Limits

* **Unsigned.** `report_sha256` and `claims_sha256` bind the answer to its inputs; they do not bind it to a time or an author. A forger who rewrites claims and report together produces a different but honest-looking report. Anchoring (a signature, a commit, a C2PA assertion) is not part of this tool. Graded Confirmed: in the x3d.ML tamper measurement, edits that leave a seam were caught in all cases, and the two families that rewrite consistently were not (0 of 160).
* **A quote inside an XML comment or CDATA section** counts as found when the scene is the source. The item's `at.attr` is empty in that case, which is the tell.
* **A shorter vector** (`2 2`) is accepted for a longer attribute (`2 2 2`), and `round_to_printed` accepts `0.12` for the quote `0.1`; both are decisions, recorded in the x3d.ML review (BUG-10, BUG-20). The `strict` policy turns the second off.
* **A vector read from prose** is matched on consecutive number tokens with any text between them, so `1 2 3` is `verified` by "1 apple, 2 pears and then 3 cows" (x3d.ML review item BUG-45, open). Read a `verified` vector from prose as weaker evidence than one copied from a scene attribute.
* **Coverage counts the inventory form of a field.** The engine counts a claim for `coverage` only when its field is the XPath `inventory` prints (x3d.ML BUG-50, open); this tool rewrites `DEF@attr`, `Tag[DEF].attr` and `n<k>@attr` to that form before the check, so its coverage is right for those forms.
* **A paraphrase is `check`.** The writer must copy; a model that cannot copy exactly gets no verified verdicts.
* **No world extents.** "6 m tall" is a product of a size and a scale; a claim about it is not checkable yet. The engine records a world box per shape; the tool does not return it.
* The answer is linear in the number of items (about 400 bytes each); for a large run use `return_mode="summary"` or `max_items`.

## Measured

One container (4 cores), Python 3.13.18, median of 7 calls after a warm-up call, wall clock, engine sha256 prefix `dcba70d7`, claims quoting a prompt that states each block's translation, `expect="inventory"`. Script: `evaluation/time_scene_evidence.py` (`python3 -I evaluation/time_scene_evidence.py`); it builds the scenes, so the sizes below are its own. Absolute numbers move between runs; the roughly linear growth with scene size does not.

| Scene | Claims | Time (with BIND) | Time (without) | Answer at the 200-item cap |
|---|---|---|---|---|
| 700 B (4 blocks) | 4 | 1.0 ms | 0.9 ms | 2,514 B |
| 92,280 B (600 blocks) | 100 | 71 ms | 66 ms | 37,988 B |
| 92,280 B | 600 | 135 ms | 129 ms | 75,543 B |
| 933,880 B (6,000 blocks) | 100 | 678 ms | 649 ms | 37,992 B |
| 933,880 B | 600 | 986 ms | 1,019 ms | 75,547 B |

BIND costs little next to parsing and indexing the scene, which is why the two columns are close. No XSD schema is compiled by this tool (unlike the first `scene_brief` call). Hostile inputs (an entity bomb, a 1 MB attribute, 3,000-deep nesting, an 800 KB source with a 600-word quote) finish within 10 seconds each in the tests, and the engine's own suite adds a nested USE-doubling scene that used to take 13 seconds at 17 levels.

## Tests

`tests/test_scene_evidence.py` (36 tests): registration and the menu entry, every `field` form, every verdict, the closed output, the injection and escape cases, the modes and blob routes, the `expect_report_sha256` recheck, and conformance rows shared with the x3d.ML engine suite.
