# Semantic twins: nine X3D scenes that answer questions and catch faults

Each case pairs an X3D scene with a domain graph written in published vocabularies. The bridge is `twin:depictedBy`, which links a domain record to the DEF'd object at `https://example.org/scen/<case>/scene.ttl#<DEF>`. The x3d_mcp tools `scene_to_rdf`, `sparql_scene` (with `data_ttl`) and `shacl_validate` (with `data_ttl`) then answer:

- three questions per case (SPARQL),
- one rule (an N3 rule, plus a property-path query that gives the same answer),
- two checks (SHACL-SPARQL), each run on a clean variant and on a variant with planted faults.

| Case | Vocabularies | Planted faults |
|---|---|---|
| heritage | CIDOC-CRM, QUDT units | Shaft drawn 12 m against 9.62 m documented; lost attribution |
| factory | AAS, SOSA, QUDT | Station moved past the robot's reach; vibration recorded in degrees Celsius |
| hospital | FHIR, BOT | Non-sterile cart inside the sterile field; C-arm record says storage |
| building | Brick 1.4.2, QUDT units | Room 303's sensor mounted in Room 304; Room 306's temperature point deleted |
| warehouse | GS1 Web Vocabulary | Chilled pallet put away in an ambient rack; pallet drawn one bin over |
| city | GeoSPARQL 1.1 | Tower 48 m tall in a 36 m district; building overhanging its parcel |
| concert | Music Ontology, Timeline, Event | Amp mic kicked downstage; keys wedge on the bass side |
| firecode | BOT, IBC 2021 values as data | Door 0.76 m wide; Exit stair B locked |
| farm | AGROVOC (live broader chains), SOSA, QUDT | Moisture probe in the wrong bed; valve missing from the irrigation record |

All 116 class and property uses are declared in each publisher's own vocabulary file, as read on 16 Nov 2026. The `twin:` terms are this example's own.

## Layout

- `scenes/`: 18 X3D 4.0 scenes (clean plus `-fault`). Every object is DEF'd and sits flat under the root, so world position equals `translation`. All 18 pass the XSD and the semantic checks with no errors or warnings.
- `data/<case>/`:
  - `domain.ttl` and `domain-fault.ttl`
  - `shapes.ttl`
  - heritage only: `shapes-strict.ttl` and `mutants.json`. A review planted eight more faults (a scaled Transform, a Cylinder for the Box, a Box with no size, feet for metres, an unrecorded object, an attribution that assigns no tier, a DEF the scene lacks, a wrapping Group); `shapes.ttl` passes all eight, the strict shapes name each, and "cannot measure" is a violation of its own instead of a pass. `tests/test_semantic_twin_strict.py` runs them. The other eight cases have not had this audit.
  - `questions.json` (the questions, the rule and the fault text)
  - `oracle.json` (the rdflib and pyshacl answers)
  - `mcp.json` (real calls to this server over stdio; arguments longer than 300 characters are stored as sha256)
- `build/`:
  - `scenes*.py` and `domain*.py` generate everything; `cases.py` merges the two rounds.
  - `oracle.py` computes the gold answers, and `record.py` replays them through the MCP server.
  - `layers.py` and `layers2.py` run the layer test below.
- `build/evidence.py` gives every claim an evidence state and writes `evidence/` (see below).
- `dl/agrovoc/chain.json`: AGROVOC broader chains fetched from the FAO REST API (AGROVOC is CC BY 4.0, FAO).
- `eval/`: `eval-set-v1.json` (45 items with gold answers) and `layers.json`.

## Run

```
cd src
python ../examples/semantic_twins/build/scenes.py && python ../examples/semantic_twins/build/scenes2.py
python ../examples/semantic_twins/build/oracle.py     # gold answers
python ../examples/semantic_twins/build/record.py     # the same calls through the MCP server
python ../examples/semantic_twins/build/layers.py && python ../examples/semantic_twins/build/layers2.py
python ../examples/semantic_twins/build/evidence.py   # evidence states; exits 1 on any mismatch
```

## Results (16 Nov 2026)

- **Oracle:** every clean case conforms, and every planted fault is caught. (Confirmed, rdflib and pyshacl.)
- **MCP server:** 18 of 18 runs match the oracle. (Confirmed, `record.py`.)
- **Browser:** N3.js, Comunica and shacl-engine match the oracle on 90 of 90 runs. (Confirmed, see the x3d.demo page.)
- **Layer test** (`eval/layers.json`):
  - Questions and rules: the scene alone answers 0 of 36, the domain records alone answer 27 of 36, and both together answer all 36.
  - Planted faults: the records alone catch 5 of 18, and the scene plus the records catch all 18.
  - The cases and faults were written by us, so read this as lift between layers on this data, not a general result.

## Evidence states (17 Nov 2026)

A bare "Confirmed" does not say whether a number was recomputed or copied. `build/evidence.py` gives each of the 238 claims one state, after the Semantic Twin Lab Evidence Edition:

| State | Meaning | Claims |
|---|---|---|
| computed here | Recomputed from the packaged files, and it agrees with the stored result | 162 |
| recorded | Taken from a stored record (the MCP run of 16 Nov, the layer test) without re-running it | 60 |
| explained difference | Recomputed, differs from the record, and the reason is known | 13 |
| hash checked | The 75 packaged files match `evidence/manifest.json` | 1 |
| not packaged | Rests on material not kept here (the browser engines, the publishers' vocabulary files) | 2 |
| mismatch | Recomputed, differs, reason unknown | 0 |

The 162 "computed here" claims are, for each of the 9 cases, 2 variants and 4 queries: the oracle answer recomputed with rdflib, and the same query through `sparql_scene` with `result_format="typed"` returning the same rows in the same order; plus the 18 SHACL verdicts.

The 13 explained differences are one finding. The server recorded on 16 Nov re-sorted every answer, so a query's `ORDER BY` was lost: in 13 of the 54 recorded answers to ordered queries, the stored row order is not the order the query asked for. The rows themselves were right. Since x3d_mcp PR #24, `sparql_scene` keeps the query's order when it has `ORDER BY` and says so in `order`.

The per-claim list is in `evidence/evidence.json`; `evidence/EVIDENCE.md` is the summary.

## Eval set

`eval/eval-set-v1.json` defines three conditions for a model:

- **A:** the scene and the tools only.
- **B:** the scene plus the records flattened to JSON, with no vocabularies, rules or shapes.
- **C:** the full ontology through `sparql_scene` and `shacl_validate` with `data_ttl`.

Scoring is precision and recall against the gold rows, plus false alarms, tool calls and tokens. It has not been run against a model yet.

## pyshacl notes found while building this

- In a SPARQL-based constraint, wrap coordinate lookups as sub-selects that project `$this`. As flat patterns, the city check took 96 s; as sub-selects it takes 0.26 s.
- A variable whose name starts with `values` is rejected as if it were a `VALUES` clause.
- Use `SELECT DISTINCT` in shapes, and avoid `GROUP BY` inside them.
