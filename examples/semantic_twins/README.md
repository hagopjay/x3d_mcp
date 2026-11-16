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
  - `questions.json` (the questions, the rule and the fault text)
  - `oracle.json` (the rdflib and pyshacl answers)
  - `mcp.json` (real calls to this server over stdio; arguments longer than 300 characters are stored as sha256)
- `build/`:
  - `scenes*.py` and `domain*.py` generate everything; `cases.py` merges the two rounds.
  - `oracle.py` computes the gold answers, and `record.py` replays them through the MCP server.
  - `layers.py` and `layers2.py` run the layer test below.
- `dl/agrovoc/chain.json`: AGROVOC broader chains fetched from the FAO REST API (AGROVOC is CC BY 4.0, FAO).
- `eval/`: `eval-set-v1.json` (45 items with gold answers) and `layers.json`.

## Run

```
cd src
python ../examples/semantic_twins/build/scenes.py && python ../examples/semantic_twins/build/scenes2.py
python ../examples/semantic_twins/build/oracle.py     # gold answers
python ../examples/semantic_twins/build/record.py     # the same calls through the MCP server
python ../examples/semantic_twins/build/layers.py && python ../examples/semantic_twins/build/layers2.py
```

## Results (16 Nov 2026)

- **Oracle:** every clean case conforms, and every planted fault is caught. (Confirmed, rdflib and pyshacl.)
- **MCP server:** 18 of 18 runs match the oracle. (Confirmed, `record.py`.)
- **Browser:** N3.js, Comunica and shacl-engine match the oracle on 90 of 90 runs. (Confirmed, see the x3d.demo page.)
- **Layer test** (`eval/layers.json`):
  - Questions and rules: the scene alone answers 0 of 36, the domain records alone answer 27 of 36, and both together answer all 36.
  - Planted faults: the records alone catch 5 of 18, and the scene plus the records catch all 18.
  - The cases and faults were written by us, so read this as lift between layers on this data, not a general result.

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
