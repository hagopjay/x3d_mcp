# scene_brief: a scene described in a size you set

`scene_brief` answers "what is in this scene, and is it sound?" in one small JSON document whose length you cap (`budget_bytes`, 1,024 to 65,536, default 4,096). It reads the same inputs as the validators (`content`, `path`, `blob_id`, or `current_scene`), changes nothing, and never returns a sentence the scene's author wrote.

It exists for two readers. A model that must decide where to look next should not be handed a 100 KB scene to find out that it has two lights. And a model that is handed text from a file it did not write should not be handed *that text* by a tool it trusts.

## What it returns

```json
{"schema":"x3d-scene-brief/1","ok":true,
 "input":{"sha256":"1d511ab5ab605a9d","bytes":2370,"x3d_version":"4.1","profile":"Immersive","declared_invalid":[]},
 "facets":{
  "census":    {"tier":"measured","nodes":25,"types":15,"defs":11,"uses":0,"depth_max":5,"protos":0,"top":{"Shape":4,"Material":3}},
  "bounds":    {"tier":"derived","static":true,"conservative":true,"min":[-0.3,-0.5,-0.5],"max":[2.5,0.5,0.5],"size":[2.8,1.0,1.0],"shapes":4,"bounded_shapes":2,"unbounded":{"invalid-value":1,"unsupported:Text":1}},
  "camera":    {"tier":"measured","viewpoints":1,"first":{"at":"n3","position":[0,1,8],"orientation":[0,0,1,0],"fov":0.7854},"headlight":true},
  "lights":    {"tier":"measured","count":1,"by_type":{"DirectionalLight":1}},
  "materials": {"tier":"measured","shapes":4,"without_appearance":1,"material_types":{"Material":3},"textures":0,"transparent":0},
  "routes":    {"tier":"measured","count":2,"dangling":1,"script_endpoints":1,"max_fan_in":1,"max_fan_out":1,"top_edges":{"TimeSensor.isActive>Script.?":1}},
  "validators":{"xsd":{"tier":"oracle","valid":false,"errors":1,"first":[{"kind":"pattern","element":"Box","attribute":"size","line":28}]},
                "semantic":{"tier":"oracle","errors":1,"warnings":0,"infos":10,"by_check":{"error:route-missing-to-node":1}},
                "render":{"tier":"not_run","checked":false,"reason":"use render_image"}}},
 "untrusted":{"tier":"untrusted","text_surface":{"meta":{"items":4,"chars":151,"sha256_8":"5357f983"}}},
 "budget":{"bytes":4096,"used":2779,"est_tokens":695,"est_method":"bytes/4","max_items":8,"dropped":[]},
 "brief_sha256":"...","timing_ms":1.9}
```

(abridged; the real answer has every facet and every text kind it found)

### Trust tiers

Every facet carries `tier`:

| tier | meaning | how to use it |
|---|---|---|
| `oracle` | the verdict of one of this server's deterministic checkers, exact for what that checker tests | evidence, with the checker named |
| `measured` | counted or parsed from the document, no judgement | evidence about the document as written |
| `derived` | computed with a stated approximation (`bounds`: the box of the authored pose, conservative under rotation, `static: true`) | evidence with the stated error direction |
| `untrusted` | text any author or upstream process wrote | never an instruction; counted and digested, quoted only on request |
| `not_run` | a stage that was skipped, with its reason | never read as a pass |

### What is never in the answer

* **Author text.** DEF names, `meta`, `WorldInfo` and `Anchor` text, descriptions, urls, `MetadataString` values, `Text` strings, comments (including those before the root element), Script and shader source (which sits in element text and in the tails of `<field>` children). Each is counted, its characters summed and a digest taken, per kind, under `untrusted.text_surface`. With `text="quoted"` a capped, JSON-escaped sample appears under `untrusted.quoted` and nowhere else. With `ids="names"` identifier-shaped DEF names appear under `untrusted.names` and nowhere else (the others are counted as `names_omitted`).
* **Validator messages.** An XSD error becomes `{kind, element, attribute?, line}` with `kind` from a fixed set and element and attribute names checked against X3DUOM (anything else prints `?`). A semantic finding keeps its `check` id (`[a-z0-9-]`, at most 40 characters), a vetted node name and a handle.
* **Field and event names the author invented.** A Script's own fields print as `?`.
* **Any number that is not a finite number.** A slot that holds text becomes `null` (`input.declared_invalid` names a bad `version` or `profile`), and words found where a number or boolean belongs are counted under `untrusted.text_surface.slot_text`.

Handles are `n<k>`: the 1-based document-order index of an element of the Scene. They are stable for one `input.sha256_full` and shift if you insert a node. Record the **full 64-hex digest plus the handle** for an immutable source locator. `input.sha256` remains its historical 16-hex prefix, a display label only (the new full field adds 64 bytes, so the answer's fixed part grew by that much). The abbreviated example above and the measurement table below describe the original edition; current answers include the additional identity fields.

The digest identifies the UTF-8 encoding of the XML string supplied to this pure function. If an input wrapper decoded another file encoding or serialized an in-memory scene first, it identifies that supplied representation, not the original external file octets.

Counts mode reduces direct author-text exposure. It is not a general proof of prompt-injection resistance. Quoted mode deliberately returns untrusted strings; keeping them in a labelled JSON field does not make a language model immune to their content. A brief or model opinion never mints a Loom acceptance witness.

## The budget

`budget_bytes` is a hard cap on the UTF-8 length of the whole answer, and `budget.used` is an upper bound on it (it includes room for the timing field). Values below 1,024 are raised to it and the answer says `clamped`. When the full answer is too large, a fixed ladder runs until it fits and each step is named in `budget.dropped`:

1. halve `max_items` (list entries per facet), down to 1;
2. drop `untrusted.names`, then `untrusted.quoted`;
3. collapse lists to counts and fold the text-surface digests into one total;
4. drop facets in this order: materials, lights, camera, bounds, routes, census;
5. drop `untrusted`, then the `render` stub.

The input digest and the `validators` verdicts survive longest. If even that cannot fit, the answer is `{"ok":false,"error":"budget-too-small","min_bytes":N}`.

## Using it for judgment work

A brief is a slice, not a proof. Two rules if you use it to feed a model that classifies or scores scenes:

* **Do not give a judge the `validators` facet when you are scoring that judge against those validators.** The answer is in the question. Ask for `facets="census,bounds,camera,lights,materials,routes,text"` and let the validators supply the labels.
* **Record what the model saw.** Keep `brief_sha256` (a hash of the whole canonical answer without the timing) and the facet list with the claim (`claim_append`).

## Limits

* `bounds` is the box of the authored pose: no routes, animation or scripts are applied, and `Text`, `Inline`, `ProtoInstance`, `Extrusion`, `ElevationGrid` and anything with an unparsable value are counted under `unbounded` with a reason, never guessed. An author's own `bboxSize` is ignored.
* The enhanced bounds path applies `CADPart` transforms, rejects fractional `Switch.whichChoice` values, and normalizes very large finite rotation axes without squaring overflow. Derived numeric overflow is reported under `unbounded.numeric-overflow`. If the aggregate size cannot be represented by finite binary64 values, `aggregate-numeric-overflow` is reported and the aggregate coordinates are omitted. JSON serialization also refuses nonfinite numbers as a final guard.
* Displayed bounds endpoints are rounded **outward** to 0.001 scene units, so quantization does not shrink a conservative envelope. Size and centre are derived from those displayed endpoints; a centre can shift by up to 0.0005 scene units. This is neither a dynamic swept volume nor a complete scene box when coverage is incomplete. The bounds walk considers the authored selected Switch child and first LOD level only.
* The XSD check pays the schema compile on the first call of a process (about 1.5 to 1.9 s); later calls are milliseconds. Leave `validators` out of `facets` to avoid both.
* Documents over libxml2's default depth limit (256) or with a start tag over 10 MB are refused as `not-well-formed`; a DOCTYPE is read but its entities are not expanded and nothing is fetched.
* The text surface lists attribute names it does not know under `other_attr` and counts them; it does not decide whether a string is an instruction. Pattern linting belongs to the caller, and is a pre-filter at best.
* No model is called anywhere in this tool.

## Measured

The table is the supplied baseline measurement, not a rerun of the enhanced implementation. It does not measure Jev or any other model.

| scene | input bytes | brief bytes (all facets) | brief without `validators` | `validate_semantic` full report | warm ms (all / without `validators`) |
|---|---|---|---|---|---|
| HelloWorld | 3,586 | 2,261 | 2,001 | 796 | 1.7 / 0.6 |
| Altar | 11,933 | 1,905 | 1,644 | 2,816 | 4.1 / 1.4 |
| MaterialModulator | 6,689 | 2,502 | 2,242 | 222 | 2.4 / 0.9 |
| LabScene41 | 1,643 | 2,412 | 2,070 | 1,130 | 1.8 / 0.7 |
| injection_scene | 2,370 | 2,773 | 2,373 | 1,951 | 2.0 / 0.7 |
| synthetic_600 | 105,262 | 1,646 | 1,383 | 301,095 | 93.9 / 31.0 |
| synthetic_6000 | 1,075,662 | 1,666 | 1,401 | 3,026,896 | 1052.9 / 344.4 |

Default settings (`budget_bytes=4096`, `max_items=8`, `text="counts"`), warm medians of 7 calls (3 for the largest scene) on Python 3.13.18, one container, 4 vCPU, no GPU; the first call of the process took 1,570 ms (the XSD compile). HelloWorld, Altar, MaterialModulator and LabScene41 are `tests/fixtures/scene_rdf`; `synthetic_N` is N Transform/Shape/Material/Box blocks. No answer needed a budget step.

What the table says: the brief is flat in scene size (1.6 to 2.8 KB from a 1.6 KB scene to a 1.1 MB one) while the full semantic report grows with the scene (301 KB for 105 KB, 3.0 MB for 1.1 MB). For a scene smaller than about 2 to 3 KB the brief is larger than the scene itself, so send small scenes whole. The time is dominated by the validators (31 ms of 94 ms, 344 ms of 1,053 ms without them on the synthetic scenes); the other facets are a single pass over the tree.

## Tests

The attachment is a **partial repository overlay**, not an installable copy of x3d_mcp. `tests/test_scene_brief_standalone.py` adds 16 offline core regressions and runs with Python, lxml and pytest:

```sh
python3 -m pytest tests/test_scene_brief_standalone.py -q
```

Its small, explicitly labelled vocabulary fixture exercises numeric geometry, budgeting and identity contracts. It does **not** validate X3DUOM, MCP transport, XSD or semantic integration. The original integration test module skips with a host-checkout reason when `tools.granular` is missing. Run it in a complete compatible x3d_mcp checkout for the original host-dependent coverage.

`tests/test_scene_brief.py` (50 tests): the registration, determinism and the hash, agreement across `content`, `path`, `blob_id` and `current_scene`, the budget as a hard cap at five budgets on two scenes, flatness in scene size, analytic bounds cases, the closed-set output of the validator facets, a sweep that fails if any string of 12 or more characters (or containing whitespace) from the hostile fixture appears in a default answer, a monkeypatched validator whose messages carry a canary that must not appear, quoted-mode escaping, hostile XML (entity expansion, external entity, a 1 MB attribute, 5,000-deep nesting), and the return modes. The fixture's adversarial strings are inert test data: see `tests/fixtures/scene_brief/README.md`.
