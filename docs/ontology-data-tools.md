# Ontology and claim data tools

Sizes behind the design are from the Playground review of this server's ontology tools
(rdflib 7.6.0, mcp 1.30.0, fork main 8dd0d3e). Grades: **Confirmed** (re-run), **Likely** (measured once).

| Observation | Grade |
|---|---|
| The largest `query_ontology` answer is the full dump, 2,675,517 B; "all datatype properties" is 644,084 B. | Confirmed |
| A tool answer costs 2.0 to 2.4 times its text on the wire; a resource read 1.05 to 1.24 times. | Confirmed |
| A real claim record is about 646 B, 65% of it hashes. | Confirmed |
| `node_parents` formatted its query with `% node_type`, so a crafted name could add clauses (including SERVICE). | Confirmed |
| The consortium query Q04 takes about 96 s on the bundled 4.0 ontology. | Confirmed |

## Limits on the old tools

`query_ontology` and `node_parents` keep their names, arguments and default answers. They now:

- parse the query first and refuse anything but SELECT and ASK, and any SERVICE or FROM clause;
- stop at a deadline: a forked worker is killed at `X3D_MCP_SPARQL_TIMEOUT` seconds (default 20; without fork the thread is only abandoned);
- cap the text at `X3D_MCP_SPARQL_MAX_CHARS` (20,000) and the rows at `X3D_MCP_ONTOLOGY_MAX_ROWS` (20,000). Past the cap the answer says `truncated` and the total;
- check that `node_type` is a plain name (letters, digits, underscore) before it goes into a query.

Q04 still exceeds the default deadline. Precomputing it as a table is not done here.

## New tools

| Tool | What |
|---|---|
| `query_ontology_page(sparql, offset, limit)` | Same guard. Rows sorted, blank nodes shown as `_:blank`, so a page has a stable `page_sha256`. The query runs once; the four most recent are cached for paging. |
| `where_can_place(child, offset, limit)` | Parents and fields that accept a node type, from the X3DUOM's `acceptableNodeTypes` (event-only fields left out), with `needsContainerField` and a versioned `table_sha256`. Unlike `node_parents` these are real placements. |
| `claim_append`, `claims_page`, `claims_verify` | JSON Lines claim ledger: canonical record, sha256, optional hash chain, graded Confirmed / Likely / Suspected, with an `instrument` object. `claims_page` takes `columns`, `hash_chars` and `drop_hashes` (a 50-row page: 27 KB full, 13 KB with 12-character hashes, 9 KB without). |
| resources `x3d-ontology://turtle` and `x3d-ontology://placement/{child}` | The ontology and a placement table without the tool-answer overhead. |

A ledger can be inline or a `blob_id`; `claim_append(store=True)` keeps the grown ledger as a blob. Blobs expire
unless the server persists them: set `X3D_MCP_BLOB_DIR` to a private directory.

## Not built

- A `load_graph` tool (none exists here; if added it needs an allowlist, a triple cap and a per-session graph).
- Precomputed Q04, a bundled 4.1 ontology, and per-release tables.
- Replacing `node_parents` (it is still the looser lattice answer).
