# Playground architecture

How the Accountability Playground is built, deployed and tested. For what the
page shows, see [GUIDE.md](GUIDE.md); for running it, [ADMIN.md](ADMIN.md).

```
 the person's browser                    the service (one container)                 cloud
 ┌───────────────────────────┐   HTTPS   ┌────────────────────────────────┐   SSE   ┌──────────────────┐
 │ playground page           │ ────────▶ │ sign-in · passes · guard       │ ──────▶ │ model endpoints  │
 │  strict CSP, cookie,      │ ◀──────── │ loop · validator report        │         │ writers A to F   │
 │  X-Playground header      │   events  │ judge compare · defect injector│  rows   ├──────────────────┤
 ├───────────────────────────┤           │ journaled store · run log      │ ──────▶ │ run log table    │
 │ viewer iframe             │ ◀──────── │ owner page                     │         ├──────────────────┤
 │  sandbox=allow-scripts    │  X_ITE    └────────────────────────────────┘ ◀────── │ signing key      │
 │  X_ITE renders, measures  │                                              at start └──────────────────┘
 └───────────────────────────┘
```

## The loop

`playground/loop.py`, one request streamed as server-sent events:

1. **Write.** The writer streams a scene (`stage`, `delta` every 120 ms, `draft`
   with tokens and estimated cost). Code fences and prose are stripped.
2. **Validate.** `report.full_report` runs the fork's validator: lxml
   well-formedness, the XSD for the declared version (`validation.validate`), and
   the semantic checks (`validation.semantic.semantic_diagnostics`). Every finding
   has a layer, a line (located from the message when the check does not report
   one), a check id, the exact message and a hint.
3. **Validator fixes.** If `validation.repair.repair_x3d` has an exact fix (a
   missing profile, an undeclared component, a version mismatch), it is applied
   and the scene revalidated. It never guesses.
4. **Writer repairs.** The writer gets the prompt, its last draft and the defect
   list (`report.defects_for_prompt`), nothing older, up to the repair limit.
5. **Final.** The run is stored, then the page gets `final` and renders it.
6. **Judge.** In parallel from step 2, a writer from another family reviews
   draft 1 with no validator and answers JSON. `judge.compare` matches its defects
   to the validator's errors by line (±1) or by a token the validator quotes.
7. **Browser.** The viewer renders the final scene and posts its frame report
   to `/api/runs/<id>/visible`. Blank or unloadable: the page asks for one
   visibility repair (`REPAIR_VISIBLE`), which continues the run on the page.

While a model is silent, the loop yields `_ping` from a queue read (never by
cancelling the run) so the response stays open. Model calls retry only before
their first token, on 429/5xx or a dropped connection, after 2, 6 and 14 s. A
closed tab cancels the run; clean-up runs in a shielded scope so the one-run
lease is released and the partial run is logged.

## The browser check

`playground/static/viewer.js` runs in an iframe sandboxed with `allow-scripts`
only, so scenes (and their Script nodes) get an opaque origin with no cookies
and no API. `viewer-shim.js` gives X_ITE in-memory storage, which an opaque
origin otherwise refuses. The check mirrors `analyze_frame` in
`src/tools/render.py`:

- wait while X_ITE reports it is loading, then sample until two frames agree
  (shaders can compile after loading ends); report blank only after 8 s;
- background = the most common colour of a 64×36 sample;
- a pixel counts when the luma of its difference from the background exceeds 12;
- under 0.05% of pixels is blank, under 1% sparse.

X_ITE is served by the service (`/xite`), fetched at build time by
`playground/fetch_xite.py` and checked against a pinned SHA-256.

## Access

`playground/auth.py` and `app.py`:

- **Pass links** `/p/<id>.<sig>`: `sig` is an HMAC-SHA256 of the id under the
  signing key. The pass record (label, expiry, own limits, admin, revoked) is in
  the store.
- **Google sign-in**: the ID token's signature, audience, issuer and verified
  address are checked server-side, then the list: blocked, addresses, admins,
  domains (suffix match, plus the Workspace `hd` claim).
- **Session**: an HMAC-signed cookie, HttpOnly, Secure, SameSite=Lax, 12 hours.
- **Every call** rechecks the pass or the address against the list (cached for
  10 s), so a revocation or a block applies at once.
- **Writes** need the header `X-Playground: 1`, which a cross-site form or a
  sandboxed scene cannot send.
- **Network limit** keys on the last `X-Forwarded-For` entry, the one the front
  end appended, stored as a keyed hash.

## Limits and spend

`playground/limits.py`, checked before every run in this order: pause, phase
spend, day spend, runs per day (everyone), runs per person per hour and per day,
runs per network per hour, one run in flight. Defaults are in `config.Limits`
and the admin page overrides any of them. Spend is estimated from tokens with
prices rounded up (`config.WRITERS`), charged when each call ends; the loop stops
between steps once a cap is reached.

## State

`playground/store.py` has one interface and three backends:

| Store | Where state lives | Use |
|---|---|---|
| `memory` | the process | tests, a laptop |
| `journal` | the process, plus every change appended to the run log as a `row_kind='state'` row and replayed at start; spend and run counts rebuilt from `row_kind='run'` rows | Cloud Run with one instance (the deployed setting) |
| `firestore` | a Firestore database | more than one instance (needs `pip install '.[playground-firestore]'` and a database) |

## The run log

`playground/runlog.py` writes one BigQuery row per run (`row_kind='run'`), per
browser verdict (`visible`) and per state change (`state`), with the
`tabledata.insertAll` REST call, so the image needs no BigQuery client. The table
schema is `runlog.SCHEMA`; create it partitioned by day on `ts` and clustered by
`row_kind, writer`. People appear in run rows as a keyed hash.

## Configuration

All settings come from the environment (`playground/config.py`):

| Variable | Default | Meaning |
|---|---|---|
| `PLAYGROUND_STORE` | `memory` | `memory`, `journal` or `firestore` |
| `PLAYGROUND_BQ_TABLE` | (off) | `dataset.table` for the run log |
| `PLAYGROUND_PROJECT` | from the runtime | cloud project for model calls and the run log |
| `PLAYGROUND_VERTEX_LOCATION` | `global` | model endpoint location |
| `PLAYGROUND_SESSION_KEY` | random per start, or stored | signing key; set it from a secret |
| `PLAYGROUND_BOOTSTRAP_PASS` | (none) | id of the owner's admin pass, created at start if missing |
| `PLAYGROUND_ADMIN_EMAILS` | (none) | comma-separated addresses that are always admins |
| `PLAYGROUND_OAUTH_CLIENT_ID` | (none) | turns on Google sign-in; also settable on the admin page |
| `PLAYGROUND_PHASE` | `2026-10` | label for the phase spend cap |
| `PLAYGROUND_USER_PER_HOUR` … `PLAYGROUND_USD_PHASE` | see `config.Limits` | limit defaults |
| `PLAYGROUND_XITE_VERSION` / `PLAYGROUND_XITE_DIR` | `16.4.3` / `playground/vendor/x_ite` | the renderer |
| `PLAYGROUND_VALIDATOR_COMMIT` | `git rev-parse --short HEAD` | shown with every verdict |
| `PLAYGROUND_INSECURE_COOKIES` | (off) | `1` for plain-HTTP local runs only |
| `OPENAI_API_KEY` | (none) | turns on Writer F |

## Running locally

```bash
pip install -e '.[playground]'
python -m playground.fetch_xite                 # X_ITE into playground/vendor/x_ite
export PYTHONPATH=src:. PLAYGROUND_SESSION_KEY=dev PLAYGROUND_BOOTSTRAP_PASS=devpass \
       PLAYGROUND_INSECURE_COOKIES=1 PLAYGROUND_PROJECT=<project>   # model calls use your cloud login
python -m playground.app                        # http://localhost:8080
python -m playground.passlink devpass           # prints /p/devpass.<signature>; open it
```

Validation, Break it and the admin page work without any cloud access; only
model runs need it.

## Deploying

```bash
PROJECT=<project> REPO=<artifact registry docker repo> OWNER_PASS=<owner pass id> \
ADMIN_EMAILS=<you@example.org> ./playground/deploy.sh
```

`deploy.sh` builds `playground/Dockerfile` with Cloud Build (uploading only what
`playground/.gcloudignore` allows) and deploys one Cloud Run service with
`--max-instances 1`, `--min-instances 0`, `--allow-unauthenticated` (the app does
its own sign-in), the run log table, the journal store and the signing key from
the secret `playground-session-key`. Mint the owner's link with
`python -m playground.passlink <owner pass id> --key-file <file holding the key>`.

The runtime identity needs: model endpoint use, BigQuery data insert and query
on the run log dataset, and access to the one secret.

## Tests

```bash
python -m pytest tests/test_playground_core.py tests/test_playground_app.py
```

70 tests: the report, access rules, passes and cookies, limits and spend, every
defect injector against the layer it claims, the judge matching, the loop with a
scripted model (repair from the exact report, validator-first fixes, repair and
spend limits, writer errors, keep-alives, early close), a restart replay of the
journal, and the HTTP service (sign-in, revocation, CSRF, owner-only routes,
security headers, a streamed run, run ownership, limits).
