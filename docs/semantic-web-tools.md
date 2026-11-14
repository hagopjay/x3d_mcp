# Semantic-web tools

Five tools put a scene into RDF and check it there, next to the XML validators:

| Tool | What it does |
|---|---|
| `scene_to_rdf(content \| path \| blob_id, format, include_ontology, base_iri, return_mode)` | X3D to Turtle (or N-Triples) by the conventions of the Web3D Consortium's `X3dToTurtle.xslt`. Keeps the graph for the session under a `graph_id`. |
| `sparql_scene(sparql, graph_id \| content..., include_ontology, ontology_version, offset, limit)` | SELECT or ASK over the scene, with the X3D Ontology joined. Guarded, deadline-bound, paged. |
| `shacl_validate(graph_id \| content..., shapes, extra_shapes_ttl, inference, ontology_version, return_mode)` | The bundled `x3d-core` SHACL shapes (or your own) on the scene. Each result names the node, its DEF, a `check_id` and a fix. |
| `rdf_infer(graph_id \| content..., profile, budget, ontology_version, sample)` | What reasoning over the ontology adds, node by node. `subclass` by default. |
| `ontology_vs_gate(child, ontology_version, xsd)` | Three answers to "where may this node go?": the ontology's `node_parents` map, the X3DUOM's `where_can_place` gate, and the XSD. |

The division of labour: **the ontology proposes, the shapes dispose, the validators gate.** An ontology answer
is never a validity verdict. `validate_x3d` (XSD) and `validate_semantic` (containerField, DEF/USE order, ROUTE
types and order, profile) stay the ground truth, and they see what RDF cannot: document order.

The `semantic_audit` prompt walks a model through the loop: `scene_to_rdf` → `sparql_scene` → `shacl_validate`
→ `validate_x3d` / `validate_semantic` → `claim_append` (and `claims_verify`), then fix and repeat.

Every answer carries an `instrument` object (see below), so a finding recorded with `claim_append` says what
produced it.

## scene_to_rdf

`src/x3d_utils/scene_rdf.py` reproduces `X3dToTurtle.xslt` (SVN r40388) in Python with lxml; no XSLT processor
is needed at run time. The stylesheet's own rule texts (which attribute values are defaults and are left out,
which field types become lists) are copied into `src/validation/schemas/X3dToTurtle-tables.json` by
`evaluation/build_scene_rdf_tables.py` and evaluated by a small XPath-subset evaluator
(`src/x3d_utils/xpath_mini.py`). The tables also hold the attribute defaults of the X3D 3.3, 4.0 and 4.1 DTDs,
because the official build reads documents with their DTD and the stylesheet sees `containerField` and the other
defaults as attributes.

Conventions of the output:

- Each element is an individual `:<name> a owl:NamedIndividual, x3do:<Element>`. The name is the DEF. A USE is a
  new individual `<DEF>-USE-<n>` with `owl:sameAs :<DEF>` and no fields. Any other node gets a positional name,
  `<Element>_<i>_<j>...`, which **changes when a node is inserted before it**; DEF names are stable.
- Parent and child are linked both ways: `:child x3do:hasParent :parent` and `:parent x3do:has<Field> :child`,
  where `<Field>` is the child's containerField, defaulted from the DTD of the document's version (3.x reads the
  3.3 DTD, 4.0 the 4.0 DTD, 4.1 or anything else the 4.1 DTD). A node that DTD does not declare (a 4.1 node in a
  4.0 document, or a prefixed element such as `<q:Box>`) has no default, and its field is named after the element,
  as the stylesheet does; a warning gives the X3DUOM's containerField.
- Non-default fields become `x3do:<field>` values: a bare number or boolean for SFBool, SFInt32, SFFloat and
  SFDouble; an `rdf:List` for the other numeric types (SFVec3f, SFColor, MFFloat, SFRotation...); a string
  otherwise (SFTime, SFString, MFString as one literal).
- ROUTEs are `x3do:hasROUTE` individuals whose `fromNode` / `toNode` are DEF strings. `head` meta become
  `x3do:hasMeta` individuals and `:meta dcterms:<name> "<content>"` triples.
- The base IRI is the `identifier` meta up to `.x3d`, plus `.ttl#`. Without one it is the stylesheet's shared
  `https://www.web3d.org/semantics/scenes/UnnamedX3dModel.ttl#`, so two unnamed scenes collide when merged
  (a warning says so). `base_iri=` overrides it.
- The namespace is `x3do: <https://www.web3d.org/specifications/X3dOntology4.1#>`, the one the stylesheet
  writes.

`graph_id` is the first 16 hex digits of the sha256 of the Turtle text. Graphs are kept per MCP session, the 8
most recent; later calls pass `graph_id=` instead of the document. `X3D_MCP_SCENE_MAX_TRIPLES` (default 300,000)
caps a conversion; `X3D_MCP_TURTLE_INLINE_MAX` (default 120,000 characters) caps the text returned inline
(`return_mode="blob"` stores the whole text).

### Fidelity

`tests/fixtures/scene_rdf/` holds seven scenes (five from the X3D Example Archives, two written for the tests)
and three with planted faults, each with the Turtle the Consortium pipeline writes for it: lxml applies the DTD,
then Saxon-HE runs `X3dToTurtle.xslt` (`evaluation/make_scene_rdf_goldens.py`; saxonche is a development tool,
not a dependency). `tests/test_semantic_web_tools.py` checks that the converter's graph is isomorphic to every
golden. The goldens can be regenerated byte for byte: set `X3D_MCP_X3DTOTURTLE_XSLT` and `X3D_MCP_X3D_DTD_DIR`
and run that test with saxonche installed.

Where the converter and the stylesheet part ways on purpose, the answer has a warning:

- A namespaced root (`<X3D xmlns='...'>`): the stylesheet matches `/X3D` without a namespace and writes no
  triples; the converter converts the scene.
- Where the stylesheet would write Turtle that no parser reads, the converter writes text that parses: a
  non-number in a numeric field is kept as a string, and a DEF with characters an IRI cannot hold (space,
  quote, backslash, controls) gets a percent-encoded IRI while its `x3do:DEF` string keeps the name.

## Ontology releases

Two releases are bundled in `src/validation/schemas/`:

| File | Source | sha256 |
|---|---|---|
| `X3dOntology4.0.ttl` | as before | `b7bb83d1...` |
| `X3dOntology4.1.ttl` | Consortium semantics, SVN r40416 | `057f1ba083a636aa50332f87fda0bfa76793876c2b14c86f51dd6db55639f2d8` |

`query_ontology`, `query_ontology_page`, `node_parents` and `describe_ontology_term` still read 4.0 by default and
give the same answers as before. `ontology_version_for(version)` picks a release for a document: 4.0 for X3D 3.x
and 4.0, 4.1 for 4.1 and for a document with no version. `sparql_scene` and `rdf_infer` use it unless
`ontology_version=` is given; when 4.0 is chosen its IRIs are rewritten into the 4.1 namespace so they join the
scene. `shacl_validate` reads 4.1 for every document version (see S6 below).

## sparql_scene

Pre-declared prefixes: `x3do:` (the 4.1 namespace; written `x3d:` instead when a query uses only `x3d:`, the
`query_ontology` habit), `rdf`, `rdfs`, `owl`, `xsd`, `dcterms`, and `:` for the scene's base. The query runs over
the scene plus the ontology (`include_ontology=true`, needed for `rdfs:subClassOf` paths), read-only.

The guard is the one `query_ontology` uses: the text is parsed first, and anything but SELECT and ASK (UPDATE,
LOAD, CONSTRUCT, DESCRIBE), any SERVICE and any FROM / FROM NAMED is refused. The query runs in a forked worker
that is killed at `X3D_MCP_SPARQL_TIMEOUT`; rows are capped at `X3D_MCP_ONTOLOGY_MAX_ROWS`. Rows are sorted, so a
page (`offset`, `limit` up to 500) has a stable `page_sha256`; the eight most recent results are kept for paging.

```sparql
SELECT ?n ?t WHERE { ?n a ?t . ?t rdfs:subClassOf* x3do:X3DGeometryNode }       # all geometry
SELECT ?use ?def WHERE { ?use owl:sameAs ?def }                                  # DEF/USE reuse
SELECT ?m ?c WHERE { ?m x3do:diffuseColor ?l . ?l rdf:rest*/rdf:first ?c }       # colour components
```

## shacl_validate

`src/validation/schemas/shapes/x3d-core.shacl.ttl`. Every shape has an `xs:checkId`, an `xs:fixHint` and a SHACL
1.2 `sh:agentInstruction`; results come back as `{focus, def, node_type, path, value, severity, message, shape,
check_id, fix_hint}`.

| Shape | check_id | Severity | Rule |
|---|---|---|---|
| S1 | `colour-out-of-range`, `value-out-of-range` | Violation | Material / PhysicalMaterial colour components and transparency, ambientIntensity, shininess, metallic, roughness in [0, 1] |
| S1b | `tuple-count` | Violation | an SFColor field has 3 components |
| S2 | `shape-no-geometry` | Warning | a Shape (not a USE copy) has geometry |
| S3 | `use-type-mismatch`, `use-undefined-def` | Violation | a USE names a DEF of the same node type that exists |
| S4 | `no-viewpoint` | Warning | the Scene has a Viewpoint (or another X3DViewpointNode) |
| S5 | `route-missing-from-node`, `route-missing-to-node` | Violation | ROUTE endpoints name DEFs in the scene |
| S6 | `field-not-in-domain` | Violation | each `has<Field>` of a node is a field the ontology gives the node's type; nodes inside a ProtoBody and Metadata directly under the Scene are exempt |
| S7 | `children-not-child-node` | Violation | nodes in a children field are X3DChildNodes |
| S8 | `duplicate-def` | Violation | one DEF names one node |

The data graph is the scene plus a slice of the ontology (the class hierarchy and the `hasChild` sub-properties
with their domains), so pyshacl does not have to load the whole ontology. S6 reads the 4.1 release for every
document version, because it lists the parent types that X3D 3.3 and 4.0 documents also use (for example for
`hasChildren`, `hasGeometry` and `hasCoord`).

`extra_shapes_ttl` adds your own shapes (x3do: is the 4.1 namespace). `owl:imports`, `sh:construct` and updates
are refused, and SPARQL constraints go through the same guard as `sparql_scene`. `shapes="none"` runs only yours.
`inference` defaults to `none`; with `rdfs`, `owlrl` or `both` pyshacl expands the scene with the whole ontology
first, and the answer carries the warning explained under rdf_infer.

What the shapes do not check, because RDF keeps no document order: USE before DEF, ROUTE before its DEFs, the
order of Switch children. Those are `validate_semantic`'s. S8 sees a DEF used twice only when the two nodes have
different parents (the second gets the same IRI).

## rdf_infer

`profile="subclass"` (default) adds each node's superclasses (`rdfs:subClassOf+`) as `rdf:type` and nothing else.
`profile="rdfs"` and `"owl-rl"` run the owlrl closure of scene plus ontology within `budget` seconds and report,
per node, how many types it had and has after. The graph kept for the session is not changed.

The ontology says which node types have a field by listing them as the property's `rdfs:domain`. RDFS and OWL read
several domains of one property as all applying at once (an intersection), so under those profiles a node that has
a field is inferred to be every listed type. The answer's warning gives the counts it measured on the bundled
release. Lab measurements (grade C, `x3d_utils.semantic_web._domain_report` and the LabScene41 fixture, rdflib
7.6.0, owlrl 7.6.2, commit of this change):

| Measurement | Value |
|---|---|
| properties with an `rdfs:domain` in `X3dOntology4.1.ttl` | 901 |
| of those, listing more than one class | 294 |
| classes listed for `x3do:DEF` | 337 |
| types per node on LabScene41, profile `subclass` (min / median / max) | 3 / 4 / 6 |
| types per node on LabScene41, profile `rdfs` (min / median / max) | 5 / 30.5 / 352 |

For class membership use `subclass`; the full profiles are for studying the ontology, not for validating a scene.

## ontology_vs_gate

Answers carry their counting rules:

- **map** = `node_parents` on the chosen release: a row is a (parent class, `has<Field>` property) whose property
  is a sub-property of `hasChild`, whose `rdfs:range` subsumes the child and is a named class other than
  `X3DNode`. Rows can repeat a pair through several ranges, so pairs are counted after de-duplication.
- **gate** = `where_can_place` on the X3DUOM 4.1: a row is a (concrete parent, SFNode/MFNode field) whose
  `acceptableNodeTypes` admits the child or a base type; inputOnly and outputOnly fields are not counted.
- Parents are distinct names. Map parents can be statements (Scene, ProtoBody) or abstract classes;
  `map_concrete_parents` counts only X3DUOM concrete nodes, and only those are compared with the XSD.
- Fields are compared by name: `has<Field>` is read as `<field>`.
- XSD verdicts: A accepts, R rejects, I the harness could not tell. Gate pairs read the shipped
  `verified-containment.json`; map-only pairs are checked live with a minimal document (profile Full, the parent
  wrapped where its own containerField needs it), at most 120 per call.

## instrument

```json
{"x3d_mcp_commit": "<git HEAD or X3D_MCP_COMMIT>", "x3d_mcp_dirty": false,
 "ontology": {"version": "4.1", "file": "X3dOntology4.1.ttl", "sha256": "057f1ba0..."},
 "shapes": {"name": "x3d-core", "sha256": "..."}, "x3duom_sha256": "...",
 "converter": {"stylesheet": "X3dToTurtle.xslt r40388", "stylesheet_sha256": "e3c5aa40...", "tables_sha256": "..."},
 "rdflib": "7.6.0", "pyshacl": "0.40.2", "owlrl": "7.6.2", "python": "3.13.x"}
```

Where the server runs without a `.git` directory (a container image), set `X3D_MCP_COMMIT` to the commit it was
built from; `x3d_mcp_dirty` is then `null`.

## Limits

- RDF has no order: Switch `whichChoice`, USE before DEF and ROUTE order are invisible to SPARQL and SHACL.
- Positional IRIs (`Shape_2_4_2`) change when the document changes; join on DEF names where they exist.
- The stylesheet is followed where it writes invalid Turtle only as far as the text must parse (warnings say
  where).
- pyshacl implements SHACL 1.0 Core and SPARQL constraints; the SHACL 1.2 annotations in the shapes file are
  carried as data.
- The query deadline uses `fork`. On a platform without it the worker is a thread that is abandoned at the
  deadline, not stopped.
- `ontology_vs_gate` with `xsd=true` validates up to 120 small documents for the pairs only the map proposes;
  `xsd=false` skips them.
