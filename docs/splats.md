# Gaussian splats

How the server handles Gaussian splat files: two tools that read a splat file on the server
(`splat_inspect`, `splat_chunk`) and a change to `validate_x3d` so that a scene too large for libxml2
gets an honest answer instead of "XML parse error". Splat files are 5 to 65 MB; none of them should
pass through the model.

This page is for whoever runs the server and for whoever writes the client. Code is in
[`src/transfer/`](../src/transfer/) (`spz.py`, `ply.py`, `x3dtext.py`, `chunk.py`, `inspect_splats.py`,
`limits.py`, `binary.py`), [`src/tools/splats.py`](../src/tools/splats.py) and
[`src/validation/oversize.py`](../src/validation/oversize.py); the module docstrings repeat what is
said here. Inputs and outputs travel through the blob store described in
[transfer-layer.md](transfer-layer.md).

Grades used below: **Confirmed** (measured or tested here), **Likely** (follows from something
Confirmed or comes from earlier work that was not repeated), **Suspected** (not checked). Every
timing is on 4 cores and 16 GB, Python 3.13, numpy 2.5, lxml 6.1 (libxml2 2.14.6), warm XSD cache
unless it says otherwise.

## Why

| Observation | Grade |
|---|---|
| A 500,000-splat scene is 49 to 65 MB as X3D text and 5 to 8 MB as `.spz`. X3D 4.1 `GaussianSplats` has only inline array fields (positions, scales, orientations, opacities, sphericalHarmonics\*), no `url`. | Confirmed |
| libxml2 refuses a start tag over 10,000,000 bytes (all attributes together). At 99 bytes per splat (lattice digits) that is 100,000 splats per node, at 129 (six decimals) 77,000, and at SH degree 3 about 18,000; the tools plan for an 8,000,000-byte budget (80,601 at lattice digits). Before this change `validate_x3d` answered `XML parse error: Resource limit exceeded`, which reads like a malformed file. | Confirmed |
| One attribute of about 1.3 million six-decimal numbers (13 MB) makes the XSD engine itself fail with "Internal error", even after the parser limit is lifted. The point where it fails depends on the shape of the numbers; three shapes were measured. | Confirmed |
| `.spz` is gzip around a fixed-point stream. Decoding 500,000 splats takes 0.08 to 0.17 s with numpy. | Confirmed |

## Quick start

Stdio (a local file):

```
splat_inspect(path="/data/pantheon.spz")
splat_chunk(path="/data/pantheon.spz", name="pantheon", out_dir="/data/pantheon-x3d")
```

Hosted (HTTP, blob store on): upload once, as in [transfer-layer.md](transfer-layer.md), then name it.

```
splat_inspect(blob_id="4f2a...")
splat_chunk(blob_id="4f2a...", name="pantheon", validate=true)
validate_x3d(blob_id="<a chunk's blob_id>", return_mode="summary")
```

Install the optional extra (the tools are not registered without numpy):

```bash
pip install 'x3d-mcp[splat]'        # numpy, and zstandard for SPZ version 4
```

The Docker image installs it. When the tools are registered: under stdio whenever numpy is
importable; over HTTP also needs the blob store on (a hosted client has no path to give).

## `splat_inspect(path="", blob_id="")`

Reads an SPZ file (versions 2, 3 and 4), a binary little-endian 3DGS PLY (SH degrees 0 to 3), or an
X3D text scene with `GaussianSplats` nodes. Format is detected from the bytes. It builds no XML:
500,000 splats take 0.3 to 0.6 s. The answer is a JSON report of a few KB:

| Key | Holds |
|---|---|
| `format`, `bytes`, `count`, `sh_degree` | what the file is; `spz` / `ply` hold header facts (version, antialiased flag, fractional bits, properties) |
| `bbox` | min, max, size, centre, the 1st to 99th percentile box, and `outlier_ratio` per axis |
| `opacity`, `scale`, `orientation`, `color_dc` | distributions and shares (invisible, under 0.05, opaque; anisotropy; flat-axis count; non-unit quaternions) |
| `x3d_text` | digits used, bytes per splat, estimated text size, `max_splats_per_node`, `single_node_checkable`, `chunks_needed` |
| `memory` | arithmetic from the layouts: float32 arrays, packed streams, sort buffers, low and high GPU estimates. Not a measurement of any renderer |
| `limits` | every threshold used by the verdict |
| `verdict` | `level` `ok`, `suspect` or `unusable`, and one reason per finding with a `code` |

Verdict rules (thresholds are defaults, echoed in `limits`; they were exercised on constructed
defects and on the real files listed below, not calibrated against rendering):

- Errors: no splats, NaN or infinity, opacity outside 0 to 1 (a logit that was not converted),
  negative scale (a log that was not converted), all splats invisible, an all-zero quaternion.
- Warnings: over half under 0.05 opacity; far floaters (bbox span over 8 times the central span);
  over 0.1% of splats larger than a quarter of the central extent; over 5% of splats with an axis
  ratio above 1000 (`needle-splats`); over 0.1% with every axis under 1e-6; quaternions not unit
  length or written w x y z; over 3,000,000 splats.
- Info: the file needs `splat_chunk` before X3D validation; SPZ and PLY record no axes
  (`coordinate-frame`); spherical harmonics present; SH degree 4 trimmed to 3.

A file it cannot decode (big-endian PLY, an ASCII PLY with elements besides `vertex`, a compressed PLY
dialect, a PLY that is not a 3DGS layout, a count above the limit) gets header facts and an `unusable` verdict with the reason;
it is never an exception.

## `splat_chunk(...)`

```
splat_chunk(path="", blob_id="", target=50000, method="kd", precision="auto",
            coordinate_system="as-is", bbox_mode="cull_safe", name="scene",
            out_dir="", url_prefix="", drop_invalid=False, validate=False, bundle=False)
```

Reads the same three formats and writes an envelope:

```
scene.x3d          master: X3D 4.1, a Viewpoint and a Group (a Transform for rdf) of one Inline per chunk
chunks/c000.x3d    one X3D 4.1 document per chunk: one GaussianSplats node with bboxCenter / bboxSize
chunks/c001.x3d    ...
manifest.json      counts, sizes, sha256 of every file, the partition, the digits, written and tight boxes
bundle.tar         only with bundle=true: a deterministic tar of the above
```

Every chunk is under 8,000,000 bytes of start tag (the limit is 10,000,000), so `validate_x3d` and
`validate_semantic` check it exactly. The chunk size is lowered from `target` when the splats carry
spherical harmonics (SH degree 3 at six decimals needs about 14,000 per chunk). The start tags are
measured after the cut and the cut is repeated at half the size if the estimate was low.

The answer lists where each file went and never carries file contents: `manifest`, `master`,
`chunk_files` (file, bytes, sha256, splats, and `blob_id` or `path`), `partition`, `digits`,
`totals`, `source` and notes. By default the files go to the blob store, and **a file's blob id is
its sha256**, the same value `manifest.json` records. With `out_dir` (stdio only) they are written
to a new or empty folder instead; nothing is overwritten and no link is followed. `blob_expires`
says when stored files lapse; fetch them (`GET /blob/<id>`) before then.

| Argument | Meaning |
|---|---|
| `target` | most splats per chunk, default 50,000 |
| `method` | `kd` (default): median cuts, exactly `ceil(n/target)` near-equal chunks. `octree`, `grid`: equal-size cells, uneven counts, more files |
| `precision` | `auto`: for SPZ, digits derived from the quantisation steps ("lattice": positions 4 decimals at 10 fractional bits, 99 bytes per splat); otherwise six decimals ("bridge"). `exact`: nine significant digits |
| `coordinate_system` | `as-is` or `rub` (the same), or `rdf`: wraps the chunks in a Transform that turns the scene half a turn about X, for y-down files. A training PLY is right-down-forward; an SPZ file records no axes |
| `bbox_mode` | `cull_safe` (default): each node's centre is its own splats' centre and its size covers the whole scene, so a viewer never culls a node. `tight`: the chunk's real box (the tight box is always in the manifest) |
| `url_prefix` | prepended to the `Inline` urls, for files hosted at an absolute address. At most 1,024 characters (it is repeated in every `Inline`); spaces, quotes, backslashes and angle brackets are refused |
| `drop_invalid` | leave out splats with NaN or infinity (default: fail and say how many) |
| `validate` | also run the XSD and semantic checks on every file and report the verdicts in `validation` (about 8 s for 500,000 splats) |
| `bundle` | also store the tar |

From an X3D text input only the `GaussianSplats` nodes are read; the answer says so. Files from a
splat source other than SPZ get six-decimal digits, so their text is larger (64.7 MB for the 500,000
splat Pantheon PLY against 49.6 MB from its SPZ).

### Why the cut is spatial

X_ITE sorts splats inside one `GaussianSplats` node, but draws the nodes themselves like transparent
shapes, far to near by the view depth of `bboxCenter`, and (earlier work) a node outside the view
frustum by its box is never sorted and holds `loadURL`. So a chunk must be a compact, disjoint block
of space with its own centre as `bboxCenter`; `cull_safe` keeps the node in view. On a stand-in
metric (the share of screen-overlapping splat pairs from different chunks that this node order draws
backwards, 28 cameras, no opacity) cutting in file order scored 20.1% on the 500,000-splat Pantheon
and a kd cut 1.1%; octree and grid scored 0.5 to 0.8%, and kd was chosen anyway for balanced chunk
sizes and the fewest files. **That metric is not a renderer; see "Rendered in a browser" below for what a
renderer showed.**

### Using the envelope in a browser

Put the files in one folder, keeping `chunks/`, and serve it from one origin (add CORS headers if the
page is on another). The `Inline` urls are relative. The blob routes need a bearer token that an
`Inline` fetch cannot send, so a hosted browser needs a static host or `url_prefix`; blob ids are for
integrity checks and for program to program transfer.

## `validate_x3d` on a scene that is too big

A document with one start tag over libxml2's limit now answers, instead of `XML parse error`:

```json
{"valid": false, "checked": false, "verdict": "not-checkable-at-this-size",
 "errors": ["Not checkable at this size: GaussianSplats (line 3, 49,137,272 bytes) is larger than libxml2's 10,000,000-byte start-tag limit, so the XSD check did not run on this document. This is not a well-formedness error."],
 "schema_version": "4.1",
 "oversize": {"tags": [{"element": "GaussianSplats", "splats": 500000, "array_check": {"errors": [], "warnings": []}}],
              "structure_check": {"ran": true, "valid": true}},
 "notes": ["Split the splats into chunks (splat_chunk ...)"]}
```

`valid` stays a bool and stays false, because nothing was proven. Two checks still run. The **array
check** applies the `validate_semantic` GaussianSplats rules (tuple counts, per-splat lengths,
partial SH degrees, negative scales, opacity range, quaternion norm and order) in numpy: 500,000
splats in about 1 s against 3.8 s for the list-walking check, with identical findings (a 400-case
differential test). The **structure check** replaces every attribute value over 64 KB with a minimal
valid value of its field type and runs the XSD on that copy, so a misspelled field, an unknown node
or a missing component is still reported with its line. Without numpy the counts are checked and the
values are reported as unchecked. Measured on the real 49 to 74 MB text scenes: 1.2 to 2.1 s warm
(about 3 s when the first validation also compiles the XSD), 255 to 352 MB peak.

A document that parses is untouched: `validate_xml` of a normal scene returns exactly what it did,
and a malformed document after an oversize one still gets `XML parse error` (the limit is read from
the exception, not from lxml's shared error log, which keeps entries across parses). Documents that
nest deeper than 256 levels or hold a text node over 10,000,000 bytes get the same kind of verdict
with their own wording and no further checks.

`validate_x3d(return_mode="summary")` carries `checked` and `verdict` when they are set.
`validate_semantic` already parses with `huge_tree` and is unchanged.

### `X3D_MCP_XML_HUGE`

Off by default. `on` (also `1`, `true`, `yes`) allows one bounded retry with libxml2's `huge_tree`
after a resource-limit failure. The retry never resolves entities, loads a DTD or touches the
network, and is **refused for any document containing `<!DOCTYPE` or `<!ENTITY`** (libxml2 expands
internal entities in attribute values even with entity resolution off, and a splat array is an
attribute). Cost, Confirmed on the 49 MB lattice scene: `valid: true` in 4.6 s at 690 MB peak (about
14 times the text), against 1.2 s and 255 MB for the verdict. The 65 MB six-decimal scene is still
not checkable with it on: the XSD engine's own failure is caught and reported (868 MB peak). Leave it
off on a hosted server (one request can cost several hundred MB; concurrent requests add up); it is
reasonable on a developer's machine. `X3D_MCP_XML_HUGE_MAX_BYTES` (default 134,217,728) is the
largest document ever retried.

## Settings

Sizes accept `K`, `M`, `G` (binary). A value that is not a number is a **startup error** naming the
variable.

| Variable | Default | Meaning |
|---|---|---|
| `X3D_MCP_SPLAT_MAX_POINTS` | 10,000,000 | most splats a file may declare. The header is checked before anything is allocated |
| `X3D_MCP_SPLAT_MAX_RATIO` | 250 | refuse an SPZ whose streams expand to more than this many times its size (any version). Real files measure 1.2 to 3; `0` switches the limit off |
| `X3D_MCP_SPLAT_MAX_PAYLOAD_BYTES` | 512M | most decoded SPZ stream bytes a file may declare (10,000,000 splats are 200 MB at SH degree 0 and 650 MB at degree 3). Checked from the header, before anything is allocated |
| `X3D_MCP_SPLAT_MAX_TEXT_BYTES` | 256M | largest X3D text scene or ASCII PLY body the tools will read (text parses to arrays larger than itself: measured peaks above the input are 4 times for X3D text with many small nodes and 5.6 times for ASCII PLY of one-digit numbers; plan for that at the cap) |
| `X3D_MCP_SPLAT_MAX_OUTPUT_BYTES` | 512M | most X3D text one `splat_chunk` call may produce, estimated from a sample before cutting; the files are held in memory until stored |
| `X3D_MCP_XML_HUGE`, `X3D_MCP_XML_HUGE_MAX_BYTES` | off, 128M | see above |

Memory, Confirmed on synthetic 10,000,000-splat files (random bytes in a valid layout; the cost
depends on the count and SH degree, not on the content): decoding SH degree 0 takes 4.8 s and peaks at
971 MB resident, `splat_inspect` 10 s and 1,687 MB; SH degree 3 (a 650 MB file) 9.0 s and 2,907 MB for
the decode, 15 s and 3,833 MB for the inspect. Those peaks include the file itself held in memory.
Scaling down linearly (Likely), the inspect needs about 150 MB per million splats at SH degree 0 and
320 MB at degree 3 on top of the file, so a host with less than 8 GB should lower
`X3D_MCP_SPLAT_MAX_POINTS`. The blob store's own per-blob cap (`X3D_MCP_BLOB_MAX_BYTES`, 256 MiB by
default) applies first to uploads.

Concurrency: both tools run in a worker thread under the same process-wide lock as other heavy tools
(see transfer-layer.md), one job at a time. Three simultaneous 500,000-splat `splat_chunk` calls and an
inspect took 6.2 s in total, about the sum of the single runs; the event loop's worst stall was 19 ms
and a small `validate_x3d` was answered at once. The first validation in a process compiles the XSD
on the event loop (1.9 s); that is not specific to splats. Peak memory was 497 MB against 251 MB for
one call, mostly the stored blobs.

## Hostile input

Files are untrusted. Confirmed by tests and a 7,500-case mutation run on the prototype (single
bytes, bit flips, truncations, header edits, valid gzip around a corrupt payload): only the readers'
own error types, at most 0.05 s and 1.2 MB per case.

- The header count is checked against `X3D_MCP_SPLAT_MAX_POINTS`, against the file size for
  unwrapped and v4 files, and against what one deflate byte can stand for (1032:1) for gzip, all
  before allocation. A header claiming 10 million splats in a 37-byte file is refused in microseconds.
- gzip is decompressed with a cap of the exact size the header implies plus one byte; a 291 KB file that
  expands to 300 MB is refused with 0.6 MB of allocations. SPZ v4: every table-of-contents size must
  match the header, and each zstd frame is capped at its expected size.
- PLY: the header is read from the first 64 KiB only; the count is checked against the body size.
- X3D text: start tags are scanned with `str.find` (a repeated-group regex cost 5.4 GB on an
  unterminated 48 MB tag), at most 256 attributes per tag.
- `out_dir` is refused over HTTP and never overwrites or follows a link; `url_prefix` and `name` are
  restricted to characters that cannot break out of the MFString or the title.

## Verified on real files

Through the registered tools (`splat_inspect`, then `splat_chunk`, then the `validate_x3d` and
`validate_semantic` tools by blob id on every file of the envelope). All eight cases below: every
file valid, no semantic errors or warnings (Confirmed; this tree, X3D 4.1 XSD).

| Source | Splats | Chunks (min..max) | Text | Notes |
|---|---:|---|---:|---|
| Pantheon SPZ v3 | 500,000 | 10 (50,000) | 49.6 MB | inspect 0.4 to 0.6 s, chunk 2.2 s, the 11 files validated in 8 s |
| Athens SPZ v3 | 497,556 | 10 (49,755..49,756) | 51.0 MB | |
| Dining room SPZ v3 | 500,000 | 10 (50,000) | 50.8 MB | |
| 84-splat SPZ v3 | 84 | 1 | 8 KB | `mostly-transparent` warning |
| Pantheon PLY (SH 0) | 500,000 | 10 (50,000) | 64.7 MB | six-decimal digits |
| Sphinx training PLY (SH 0) | 408,711 | 9 (45,412..45,413) | 54.1 MB | |
| Synthetic PLY, SH degree 3 | 132,923 | 10 (13,292..13,293) | 74.2 MB | chunk size lowered from 50,000 to 14,334 by the 8 MB budget |
| 892-splat PLY | 892 | 1 | 116 KB | |

Decoder cross-checks (Confirmed): against the decoder in the earlier splat-build work, positions,
opacity, colour and scale are identical and rotations agree to 2.3e-5 degrees; against the earlier
SPZ-to-PLY converter, opacity to 1e-6, colour to 2.2e-7, scale to 1.4e-7 relative. Re-packing a
decode reproduces every position, alpha, colour and scale byte (100%) and 97.3% (Pantheon) and
95.4% (Athens) of the rotation words; the rest differ by at most 0.112 degrees (a rounding tie or a
different largest component, not a reader error). Lattice-digit text read back and re-packed gives
the same integers.

The inspect rules were also run over every SPZ and PLY file found in the project (23 files, 9 SPZ
and 14 PLY; a few are copies of each other). Nineteen get `ok`, among them the two ASCII PLY files (16 and 50,058
splats); the 84-splat SPZ gets `mostly-transparent`;
three trained PLY files (60,163 to 457,385 splats) get `needle-splats`, with 11.6% to 14.8% of their
splats over 1000:1 (the Pantheon PLY has 0.0%, the 408,711-splat Sphinx PLY 3.6%). Whether those
render badly is not known, so read that warning as "look", not "bad".

## Rendered in a browser

Chunked envelopes were loaded from a static folder in X_ITE 16.5.0 (headless Chromium, software
WebGL2) and compared with one `GaussianSplats` node holding the same splats, from fixed viewpoints
inside the scene (PSNR, 8 bit RGB, higher is closer). The sources were the real Pantheon files
(50,058-splat PLY cut at 10,000 per chunk, six views; 500,000-splat SPZ cut at 10,000 per chunk, two
views). Each run is one render per view, so differences of a dB or so are not established.

| Cut | 50k, mean of 6 | 500k, mean of 2 |
|---|---|---|
| kd, `cull_safe` (default) | 42.4 | 36.4 |
| octree, `cull_safe` | 42.6 | 37.2 |
| grid, `cull_safe` | 41.9 | 33.4 |
| file order, `cull_safe` | 38.1 (25.9 to 62.6 by view) | 21.4 |
| kd, `tight` | 42.4 (identical images) | did not load (below) |

- **Spatial cuts beat file order**, which is the design's claim, and by a wide margin at 500k. kd,
  octree and grid are close at 50k; at 500k grid trailed by about 3 dB on two views. The kd default
  stands, but this does not separate it from octree.
- **`tight` at 500,000 splats did not finish loading** within 90 s and 300 s in two runs, where
  `cull_safe` loaded in about 6.6 s. At 50,000 splats and in one view from outside the scene the two modes
  were identical. This fits the earlier claim that a culled node can hold `loadURL`, but the cause
  was not isolated (Likely, not Confirmed). It supports keeping `cull_safe` as the default. A
  hand-edited variant with no `bboxCenter` at all (not a tool option) also loaded fine at 50k.
- **Orientation:** the real SPZ rendered with `coordinate_system="rdf"` is upright (coffered ceiling
  at the top); written `as-is` it renders upside down (13.5 dB against the rdf render). SPZ and PLY
  record no axes, so pass `rdf` for files from the usual capture tools and check one render.
- Whether the original claim that file-order chunks are drawn out of depth order is the reason for
  the file-order loss was not isolated.

## Not verified

- **Other browsers and renderers.** The render check below used one headless Chromium (software
  WebGL2) and X_ITE 16.5.0 (16.4.3 agreed on the smaller checks), two to six fixed viewpoints, and
  PSNR against a single-node reference. It says nothing about GPUs, other X_ITE versions, X3DOM or
  other viewers, and PSNR is a crude measure of splat sorting.
- **SPZ versions 2 and 4 and SH degrees 1 to 3 in SPZ.** All nine real SPZ files found are version 3
  with SH degree 0. The other paths follow the reference source and are tested against files this
  repository's test writer builds, which follows the same source. The SH layout (`[coefficient][R G B]`
  per splat) is the one most likely to be wrong. Real PLY files do exercise SH degree 3.
- **Compressed PLY dialects and big-endian PLY** are recognised and refused with a named reason.
- **ASCII PLY** is read (whitespace-separated numbers, the vertex element only) and checked against its binary twin
  on the two ASCII files found in the project (positions agree to the printed precision, 5.0e-4 at most
  on the 50,058-splat file). Both files came from one producer; other writers' number formats
  (`1e-3`, `nan`, `inf`, `+1`) rely on numpy's float parser and were not tried on real files.
- **The inspect thresholds** separate constructed defects from the real files; whether they separate
  bad scenes from good ones is not known.
- **The XSD engine's failure point** was measured for three number shapes. The code does not depend on
  the figure: it catches the engine's error.
- **Text input is lossy for non-splat content.** `splat_chunk` of an X3D text scene keeps only its
  GaussianSplats nodes; transforms around them are not applied.

## Tests

`tests/test_splat_spz.py`, `test_splat_ply.py`, `test_splat_chunk.py`, `test_splat_inspect.py`,
`test_splat_arrays.py`, `test_splat_tools.py`, `test_splat_http.py`, `test_validation_oversize.py` and
`test_inprocess_signatures.py` (which pins the signatures `playground/report.py` and the tools call in
process). They need numpy (the `dev` extra installs it) and use synthetic files. The real-file tests
run when `X3D_SPLAT_FIXTURES` names a folder holding `pantheon.spz`, `athens.spz` and `txt/` (the
text scenes), and `X3D_SPLAT_PLY` a 3DGS PLY with SH degree 3; otherwise they are skipped.
