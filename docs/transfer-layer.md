# Transfer layer

How large documents get into and out of the tools without passing through the
model. Today every tool takes `content: str`, so a 10 MB scene means a 10 MB
`tools/call` (and, for the tools that return a document, a 10 MB answer). The
transfer layer adds a small content-addressed **blob store** and lets the tools
name a document by id instead.

This page is for whoever runs the server. Code is in [`src/transfer/`](../src/transfer/)
(the module docstrings repeat the settings below) and [`src/tools/transfer.py`](../src/tools/transfer.py).

Grades used below: **Confirmed** (measured or tested here), **Likely** (follows from
what was measured), **Suspected** (not checked).

## Why

| Observation | Grade |
|---|---|
| With mcp 1.30.0, a `tools/call` body over 4 MiB is answered `413 Request body too large` from `Content-Length` before any tool runs; 1 MB and 3.5 MB are accepted. mcp 1.28 and earlier have no cap. | Confirmed |
| Of ten Shroud scenes, seven fit under that cap; the three largest (4.8, 9.2 and 10.9 MB) and every splat scene (49 to 65 MB of text) do not. | Confirmed |
| `path=` is refused over HTTP, so a hosted server had no way to take a large file at all. | Confirmed |
| A `-> str` tool result is sent twice (`content[].text` and `structuredContent.result`), so a 10 MB result is 20 MB on the wire. The four blob tools use `structured_output=False`; the existing tools are unchanged. | Confirmed |
| Synchronous tools run on the event loop: while a 100 MB `validate_x3d` ran, nothing else was answered for 6.7 s. Documents of 1 MiB or more now run in a worker thread. | Confirmed |

## When the blob store is on

| Transport | Store | HTTP routes | `blob_*` tools and the `blob_id` / `return_mode="blob"` parameters |
|---|---|---|---|
| stdio (default) | on, no token needed (the process belongs to the user) | none | on |
| streamable-http, no `X3D_MCP_BLOB_TOKEN` | **off** | not registered | tools not registered; the parameters stay on the existing tools and answer "blob store is not enabled" |
| streamable-http, token set | on | `/blob` (bearer required) | on; the MCP connection must also carry the bearer unless `X3D_MCP_BLOB_TOOLS_OPEN=1` |

`X3D_MCP_BLOB=off` forces everything off. `X3D_MCP_BLOB=on` over HTTP without a token, or a
token shorter than 32 characters, is a **startup error** (there is no way to switch on
unauthenticated routes). With the store off, nothing about the server changes and a call that
asks for `blob_id` or `return_mode="blob"` gets "blob store is not enabled" back as an
ordinary tool error.

Why the tools also check the bearer: `/mcp` itself has no authentication, so without the
check anyone who can reach `/mcp` could fill the store.

## Quick start

Hosted (HTTP):

```bash
export X3D_MCP_BLOB_TOKEN="$(python -c 'import secrets; print(secrets.token_urlsafe(48))')"
export X3D_MCP_ALLOWED_HOSTS="x3d.example.com"            # see "Security notes"
MCP_TRANSPORT=streamable-http PORT=8000 python src/server.py
```

Upload once, compressed (a program does this, not the model):

```bash
zstd -3 -q scene.x3d -o scene.x3d.zst
curl -sS -X POST "https://x3d.example.com/blob" \
  -H "Authorization: Bearer $X3D_MCP_BLOB_TOKEN" \
  -H "Content-Type: model/x3d+xml" -H "Content-Encoding: zstd" \
  --data-binary @scene.x3d.zst
# {"blob_id":"4f2a...","sha256":"4f2a...","size":10873411,"stored_size":429817,"encoding":"zstd",...,"created":true}
```

Then the MCP client (configured to send `Authorization: Bearer ...` on `/mcp`) names it:

```
validate_x3d(blob_id="4f2a...", return_mode="summary")
modify_x3d_node(content="", blob_id="4f2a...", def_name="Lamp", field_changes="{...}", return_mode="blob")
```

Under stdio there is no upload route; pass `path=` as before, or `blob_put(path="/abs/scene.x3d")`
to get an id you can reuse.

## Settings

Everything is an environment variable with a default. Sizes accept `K`, `M`, `G` (binary).

| Variable | Default | Meaning |
|---|---|---|
| `X3D_MCP_BLOB` | `auto` | `auto`, `on` or `off` (see above) |
| `X3D_MCP_BLOB_TOKEN` | unset | Bearer token, 32 characters or more. Read from the environment only; never logged or returned |
| `X3D_MCP_BLOB_TOOLS_OPEN` | `0` | `1`: the MCP tools need no bearer (for clients that cannot set headers on `/mcp`). Blob ids are then capabilities and anyone who can reach `/mcp` can store blobs |
| `X3D_MCP_BLOB_MAX_BYTES` | `256M` | Largest blob, decoded, and largest upload on the wire |
| `X3D_MCP_BLOB_MEM_BYTES` | `128M` | Stored (compressed) bytes kept in RAM |
| `X3D_MCP_BLOB_QUOTA_BYTES` | `1G` with a spill directory, else the RAM budget | Total stored bytes. Past it, least recently used blobs are evicted; a blob being read or downloaded is never evicted; if nothing can be evicted the upload gets 507 |
| `X3D_MCP_BLOB_DIR` | unset | Spill directory for blobs beyond the RAM budget. Created mode 0700 if missing; an existing one must be a real directory (not a symlink), owned by the server user and mode 0700, or the server refuses to start. Unset over HTTP: RAM only. Under stdio a private temporary directory is made on first spill and removed at exit |
| `X3D_MCP_BLOB_HOT_BYTES` | `64M` | Decoded copy kept for paged `blob_read` calls; `0` disables |
| `X3D_MCP_BLOB_TTL` | `3600` | Idle seconds before a blob expires; every use renews it |
| `X3D_MCP_BLOB_MAX_AGE` | `86400` | Hard lifetime in seconds, whatever the use |
| `X3D_MCP_BLOB_MAX_RATIO` | `250` | Largest decoded:wire ratio once 1 MiB has been decoded (decompression-bomb guard); `0` disables |
| `X3D_MCP_BLOB_MAX_UPLOADS` | `4` | Concurrent streaming uploads before 429 |
| `X3D_MCP_BLOB_IDLE_TIMEOUT` | `30` | Seconds without upload data before 408 |
| `X3D_MCP_BLOB_TOTAL_TIMEOUT` | `600` | Seconds for one whole upload |
| `X3D_MCP_BLOB_INLINE_MAX` | `3000000` | Largest decoded `blob_put` payload (its base64 form and the JSON-RPC envelope must stay under the SDK's 4 MiB cap) |
| `X3D_MCP_BLOB_PAGE_DEFAULT` / `_PAGE_MAX` | `16384` / `262144` | `blob_read` page size and its ceiling |
| `X3D_MCP_ALLOWED_HOSTS` / `X3D_MCP_ALLOWED_ORIGINS` | unset | Comma lists (SDK grammar, `host:*` allowed). When set, the SDK's Host/Origin checks apply to `/blob` **and** `/mcp` |
| `X3D_MCP_SUMMARY_ITEMS` | `200` | Most list entries a `return_mode="summary"` answer carries |
| `X3D_MCP_OFFLOAD_MIN` | `1M` | Documents at least this large are processed in a worker thread, one job at a time |

A bad value (not a size, below the minimum, unknown mode) stops the server at startup with a
message naming the variable.

## HTTP API

Routes exist only under HTTP with a token. Every one needs `Authorization: Bearer <token>` and
passes the Host/Origin check. Metadata answers are JSON with `Cache-Control: no-store`.

```
POST|PUT /blob            upload; the server computes the id            201 created | 200 already stored
PUT      /blob/{sha256}   upload; the client states the id              201 | 200 | 422 if the bytes differ
GET      /blob/{sha256}   the bytes                                     200 | 404
HEAD     /blob/{sha256}   headers only                                  200 | 404
DELETE   /blob/{sha256}                                                 204 | 404
```

Upload headers: `Content-Type` is the media type (`model/x3d+xml`, `application/json`,
`text/turtle`, ...; anything unrecognised is stored as `application/octet-stream`);
`Content-Encoding` is one of `identity` (default), `gzip`, `br`, `zstd`; optional `X-Blob-Name`
(a display hint, never used in a path) and `X-Blob-Sha256` (the expected id of the decoded bytes).
`Content-Length` is optional; chunked bodies stream. A `PUT /blob/{sha256}` for an id that is
already stored is answered `200` before any body byte is read.

Response: `{"blob_id","sha256","size","stored_size","encoding","media_type","expires","created_at","ratio","created", "name"?}`
where `size` is the decoded size, `stored_size` what the store holds, `encoding` how it holds it.
The id is the lowercase hex SHA-256 of the decoded bytes, so equal content has one id whatever
codec it travelled in. Tool parameters also accept `sha256:<hex>`.

Download: `Accept-Encoding` is honoured (q-values, `*`). If the stored codec is acceptable the stored
bytes go out as they are with `Content-Encoding`; otherwise the blob is decoded in 1 MiB pieces and
sent plain. Headers: `ETag`, `Vary`, `X-Blob-Size`, `X-Blob-Stored-Size`, `X-Blob-Codec`,
`Content-Disposition: attachment`, `Content-Security-Policy: sandbox`, `X-Content-Type-Options: nosniff`.

| Status | When |
|---|---|
| 400 | id is not 64 hex characters; corrupt, truncated or trailing-data compressed stream |
| 401 | missing or wrong bearer (`WWW-Authenticate: Bearer realm="x3d-blob"`) |
| 403 / 421 | Origin / Host not allowed (the SDK's own answers) |
| 404 | unknown or expired id |
| 408 | no data for `IDLE_TIMEOUT`, or an upload longer than `TOTAL_TIMEOUT` |
| 413 | declared length, wire bytes or decoded size over `MAX_BYTES`, or ratio over `MAX_RATIO` |
| 415 | unsupported or stacked `Content-Encoding` (the body lists what is supported) |
| 422 | stated id differs from the SHA-256 of the decoded bytes (body has `expected` and `actual`) |
| 429 | more than `MAX_UPLOADS` uploads in flight (`Retry-After: 2`) |
| 507 | quota or memory budget exhausted and nothing evictable |

Checks run cheapest first and before the body is read: Host/Origin, bearer, id syntax, known id,
`Content-Encoding`, declared length, concurrency, quota reservation.

## MCP tools

New tools (registered only when the store is on):

| Tool | Does |
|---|---|
| `blob_put(content, encoding="utf-8", media_type, content_encoding, name, path, ttl)` | Store a small document (at most `INLINE_MAX` decoded bytes). `encoding="base64"` for binary or compressed data, `content_encoding` `gzip`/`br`/`zstd` when the base64 is a compressed stream. `path` reads a local file under stdio only |
| `blob_info(blob_id)` | Size, stored size, codec, media type, expiry, and a 200-character preview for text |
| `blob_read(blob_id, offset, length, encoding)` | A window of the decoded bytes. Text pages never split a multibyte character; page with `next_offset` until `eof` |
| `blob_delete(blob_id)` | Drop it now |

Existing tools gain two optional parameters, appended after the old ones, so every existing call
means what it meant:

- `blob_id` (default `""`): a stored document, used instead of `content` or `path`. Exactly one of
  the three may be given; more than one is an error that names them. Where `content` is required
  (`convert_x3d`, `modify_x3d_node`, `remove_x3d_node`, `move_x3d_node`) pass `content=""`.
- `return_mode` (default `"full"`): `"full"` is today's output, byte for byte. `"summary"` returns a
  small JSON object instead of the document. `"blob"` stores the result and returns its id in the
  same kind of summary, so the next call can name it.

Tools: `validate_x3d`, `validate_semantic`, `autofix_x3d`, `repair_x3d`, `convert_x3d`,
`modify_x3d_node`, `remove_x3d_node`, `move_x3d_node`. `convert_x3d` reads either encoding from a
blob (`from_encoding="xml"` or `"json"`), so a scene a model wrote as JSON can be stored once and
converted, validated and edited by id. `x3d_generation_schema` takes `return_mode` only: `"summary"`
returns its report, notes and the schema's `sha256` and size, `"blob"` also stores the schema
(`application/schema+json`) and returns its `blob_id`, so the program that calls the model fetches it
over `GET /blob/<id>` and the schema never passes through the model's context.

Tools that produce a document (`autofix_x3d`, `repair_x3d`, `convert_x3d`, the three edit tools)
answer a summary with `ok`, `source`, `input` (`sha256`, `bytes`), `document` (`sha256`, `bytes`,
`nodes`, `node_types`, and `blob_id` in blob mode) and `changes`. `validate_x3d` and
`validate_semantic` produce a report rather than a document: their summary has the counts and
findings, and in blob mode a `report` entry holding the stored report's `blob_id`. Every list is
capped at `X3D_MCP_SUMMARY_ITEMS` (`<name>_total` and `<name>_truncated` say when) and long strings are
clipped. A failure is JSON `{"ok": false, "error": ...}` in the new modes and the tool's usual text in
`full`. A summary of an 8000-node, 1.4 MB scene edit is under 1.5 KB (Confirmed, tests).

Blob input is always a document: a blob that begins with a comment or a byte-order mark is not
mistaken for a file path. (Inline `content` keeps its old rule, so inline text that does not start
with `<?xml`, `<X3D` or `<!DOCTYPE` is still treated as a path by the scene-editing tools.)

## Compression at rest

| Upload | Stored as |
|---|---|
| identity, gzip | re-encoded to zstd level 3, streaming (gzip level 6 if the `zstandard` package is absent) |
| br, zstd | exactly as sent |
| anything where a 256 KiB zstd-1 probe shrinks it by less than 5% (an `.spz` file) | as is |

Measured on a 10.9 MB scene: zstd-3 gives 25x in 15 ms; zstd-19 gives 44x in 2.6 s; brotli-11 gives
35x in 12.6 s; gzip gives 5x (Confirmed, prototype). Levels 5 to 15 of zstd were worse than 3 on
that file, so there is no "middle" setting. Decoding is 5 to 8 ms (zstd) to 36 ms (gzip) for 10 MB.

**What clients should send:** zstd level 3 (brotli 5 if zstd is not available, gzip 6 as the
universal fallback; identity is accepted). For a 64.7 MB splat scene zstd-3 was the fastest total
at 10 Mbit/s, 100 Mbit/s and 1 Gbit/s (12.5 s, 1.7 s, 0.65 s) because zstd-19 costs 55 s of client
CPU to save 4.5 MB. Use zstd-19 or brotli-11 only for `blob_put` base64, where bytes are metered as tokens.

The `blob` extra installs the codecs (`pip install 'x3d-mcp[blob]'`, done in the Dockerfile). Without
`brotli` (1.2.0 or newer, the first with bounded output) `br` uploads get 415; without `zstandard`
`zstd` gets 415 and the store uses gzip.

## Security notes

- **Token.** `X3D_MCP_BLOB_TOKEN` is compared in constant time (`hmac.compare_digest` over SHA-256
  digests, so lengths do not leak). The scheme must be `Bearer`; `Basic` with the right token is
  refused. By construction only: no timing experiment was run (Suspected that it is not measurable
  over a network).
- **Host and Origin.** The routes reuse the SDK's `TransportSecurityMiddleware`. A loopback bind has
  the checks on already. On a hosted bind set `X3D_MCP_ALLOWED_HOSTS` (and `_ORIGINS` if a browser
  page calls the server); they now also apply to `/mcp`, which had none. Without them the bearer
  alone protects `/blob` and the server logs a warning at startup. No CORS headers are ever sent, so
  a cross-site page cannot attach the `Authorization` header.
- **TLS** is the host's job (Render, Cloud Run, a reverse proxy). Do not use the token over plain
  HTTP across a network.
- **Ids** can only be 64 hex characters; anything else is rejected before it reaches the table or the
  file system, so `..`, `/`, encoded slashes and `sha256:` tricks cannot name a file. Spill files are
  named by id only, in a directory the server owns; at startup only the server's own hex-named files
  there are cleaned.
- **Downloads** are attachments with `nosniff` and `Content-Security-Policy: sandbox`, so an uploaded
  `.x3d` or HTML file cannot run in the server's origin.
- **Bombs.** Every compressed body goes through a bounded decoder: output limited per call, a decoded
  size cap (`MAX_BYTES`), a ratio limit, and rejection of truncated or trailing data. A 50 MiB stream
  of zeros compressed to a few kilobytes is refused (Confirmed, tests).
- **Abuse.** Streaming (no `request.body()`), at most `MAX_UPLOADS` in flight, idle and total timeouts,
  reserve-then-grow accounting so a failed or abandoned upload gives back its quota and leaves no
  temporary file (Confirmed, tests with a client that disconnects and one that stalls).
- **Single tenant.** One token, one namespace: a holder can read and delete any blob and can evict
  others by filling the quota. Run one token per deployment.
- **`blob_put(path=)`** reads local files, so it is refused whenever HTTP is configured.

## Deployment notes

- **One process, one store.** Nothing is persisted; a restart empties it. Two replicas or several
  uvicorn workers each have their own store, so an id uploaded to one is a 404 on the other. Use one
  instance, or sticky routing.
- **Memory.** Baseline process plus `MEM_BYTES` plus working space: a streaming upload adds roughly
  10 to 20 MB, and a tool that processes a blob holds the decoded document (plus lxml's tree). At 25x
  a 128 MiB budget holds about 3 GB of scene text. Concurrent 100 MB uploads were not measured
  (Likely about 20 MB each).
- **Loopback only.** Every measurement here was on loopback. Hosting limits in front of the app
  (request-size caps, idle timeouts, proxy buffering) were not checked (Suspected to matter): try one
  100 MB `PUT` through the real front end before relying on it.
- **Heavy tools.** Documents of `OFFLOAD_MIN` or more run in a worker thread under one process-wide
  lock, so two large validations queue rather than run side by side. The loop keeps serving. Whether
  the X3D library underneath is safe to call from two threads at once was not established, which is why
  they do not.
- **Limits that remain.** libxml2 refuses a start tag over 10 MB, so a splat scene stored as one
  huge attribute still cannot be parsed, and the XSD check gives an "Internal error" on very large
  scenes. Neither is changed here; the blob store only gets the bytes to the tool.

## Not built

Left out on purpose; each is a compatible later addition. Fetching a blob from a URL
(`blob_fetch`, with an allowlist and a pinned address), short-lived download tickets, `blob_export` to a
local path, a background pass that recompresses at zstd-19, a store-statistics route, per-token
namespaces, a shared backend for several replicas, and the `blob_id` / `return_mode` parameters on
`animate`, `add_route`, `render_image` and `x3dom_page`.
