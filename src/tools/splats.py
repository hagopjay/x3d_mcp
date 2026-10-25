"""Gaussian splat tools: splat_inspect and splat_chunk.

A splat file is the wrong thing to pass through a model. A 500,000-splat scene is 5 to 8 MB as an
SPZ file and 49 to 65 MB as X3D text, and X3D 4.1 GaussianSplats has only inline array fields, so
one node holds the whole cloud in a single attribute set that libxml2 will not parse past 10 MB.
These tools take the file by `blob_id` (or `path` under stdio), decode it on the server, and return
a report or the ids of the pieces; the model never sees the data.

  splat_inspect  count, SH degree, bounding box, statistics, memory estimates and a verdict. No XML is built.
  splat_chunk    cuts the cloud into spatial chunks and writes an envelope: a master scene of Inline nodes,
                 one X3D file per chunk (each small enough for validate_x3d) and a manifest with sha256 digests.

The readers, the chunker and the sanity rules are in src/transfer (spz, ply, chunk, inspect_splats);
the limits (X3D_MCP_SPLAT_*) are in transfer.limits. They need numpy, an optional extra
(`pip install 'x3d-mcp[splat]'`); without it these tools are not registered.
"""

import hashlib
import json
import math
import os
from pathlib import Path

from lxml import etree
from mcp.server.fastmcp import Context, FastMCP

from transfer.auth import tool_gate
from transfer.binary import resolve_bytes_input
from transfer.blobs import get_config, get_store, transport_is_http
from transfer.limits import SplatLimits
from transfer.outputs import cap_items, clip
from transfer.tooling import run_heavy

_NO_NUMPY = "the splat tools need numpy: pip install 'x3d-mcp[splat]'"
_X3D_TYPE = "model/x3d+xml"
_MEDIA = {".x3d": _X3D_TYPE, ".json": "application/json", ".tar": "application/x-tar"}


def numpy_available() -> bool:
    """Whether the splat tools can run (they are registered only when this is true)."""
    try:
        import numpy  # noqa: F401
    except ImportError:
        return False
    return True


def _err(message) -> str:
    return json.dumps({"error": str(message)}, indent=2)


def _finite(obj):
    """`obj` with every non-finite float replaced by a string, so the report is strict JSON."""
    if isinstance(obj, float) and not math.isfinite(obj):
        return "nan" if math.isnan(obj) else ("inf" if obj > 0 else "-inf")
    if isinstance(obj, dict):
        return {k: _finite(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_finite(v) for v in obj]
    return obj


def _always() -> int:
    """A `size` for run_heavy that always offloads: a splat file decodes to many times its own size."""
    return get_config().offload_min


# ---------------------------------------------------------------- jobs (run in a worker thread)

def _inspect_job(data: bytes, limits: SplatLimits) -> dict:
    from transfer.inspect_splats import inspect_bytes
    return inspect_bytes(data, max_points=limits.max_points, max_text_bytes=limits.max_text_bytes,
                         max_ratio=limits.max_ratio)


def _validate_files(files: dict[str, bytes]) -> dict:
    """XSD and semantic verdicts for every X3D file of an envelope (about 10 s for 500,000 splats)."""
    from validation.semantic import NoSceneError, semantic_diagnostics
    from validation.validate import validate_xml
    failures, xsd_ok, sem_errors, sem_warnings = [], 0, 0, 0
    names = sorted(p for p in files if p.endswith(".x3d"))
    for path in names:
        text = files[path].decode("utf-8")
        xsd = validate_xml(text)
        try:
            diags = semantic_diagnostics(text)
        except (etree.XMLSyntaxError, NoSceneError) as exc:
            failures.append({"file": path, "xsd_errors": [], "semantic_errors": [clip(str(exc))]})
            continue
        errs = [d for d in diags if d.level == "error"]
        xsd_ok += bool(xsd["valid"])
        sem_errors += len(errs)
        sem_warnings += sum(d.level == "warning" for d in diags)
        if not xsd["valid"] or errs:
            failures.append({"file": path, "xsd_errors": [clip(e) for e in xsd["errors"][:3]],
                             "semantic_errors": [clip(f"[{d.check}] {d.message}") for d in errs[:3]]})
    return {"files": len(names), "xsd_valid": xsd_ok, "semantic_errors": sem_errors,
            "semantic_warnings": sem_warnings, "ok": not failures, "failures": failures[:10]}


def _check_out_dir(out_dir: str) -> Path:
    """The folder to write into: it must not exist yet, or be empty."""
    root = Path(out_dir).expanduser()
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError(f"out_dir {root} exists and is not an empty folder; pass a new or empty one")
    return root


def _write_dir(files: dict[str, bytes], out_dir: str) -> str:
    """Write the envelope into a new or empty folder; nothing is overwritten and no symlink is followed."""
    root = _check_out_dir(out_dir)
    try:
        for rel in sorted(files):
            dest = root / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0), 0o644)
            with os.fdopen(fd, "wb") as f:
                f.write(files[rel])
    except OSError as exc:
        raise ValueError(f"could not write {out_dir}: {exc}; files already written were left there") from exc
    return str(root.resolve())


def _store_blobs(files: dict[str, bytes]) -> dict[str, dict]:
    """Put every file in the blob store; the blob id of a file is its sha256, which the manifest also records."""
    store = get_store()
    return {rel: store.put(data, media_type=_MEDIA.get(Path(rel).suffix, "application/octet-stream"), name=rel)
            for rel, data in sorted(files.items())}


def _chunk_job(data: bytes, limits: SplatLimits, opts: dict) -> dict:
    """Decode, cut, optionally validate, then store or write. Returns the JSON-ready result."""
    from transfer.chunk import build_envelope, to_tar
    from transfer.inspect_splats import load_cloud
    cloud, fmt, facts = load_cloud(data, max_points=limits.max_points, max_ratio=limits.max_ratio,
                                   max_text_bytes=limits.max_text_bytes)
    source = {"format": cloud.source_format, "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()}
    files, manifest = build_envelope(
        cloud, name=opts["name"], target=opts["target"], method=opts["method"], precision=opts["precision"],
        coordinate_system=opts["coordinate_system"], bbox_mode=opts["bbox_mode"], url_prefix=opts["url_prefix"],
        drop_invalid=opts["drop_invalid"], source=source, max_total_bytes=limits.max_output_bytes)
    del cloud
    notes = []
    if fmt == "x3d-xml":
        notes.append(f"Only the GaussianSplats nodes were read from the X3D text ({facts.get('nodes', 0)} of them); any other "
                     "node, and any Transform around the splats, is not in the envelope.")
    result = {"name": manifest["name"], "source": manifest["source"], "partition": manifest["partition"],
              "digits": manifest["digits"], "coordinate_system": manifest["coordinate_system"],
              "bbox_mode": manifest["bbox_mode"], "totals": manifest["totals"]}
    if opts["validate"]:
        result["validation"] = _validate_files(files)
        if not result["validation"]["ok"]:
            notes.append("validation found problems; see validation.failures")
    if opts["bundle"]:
        files = dict(files, **{"bundle.tar": to_tar(files)})
    if opts["out_dir"]:
        root = _write_dir(files, opts["out_dir"])
        place = {rel: {"path": str(Path(root) / rel)} for rel in files}
        result.update(stored="directory", out_dir=root)
    else:
        metas = _store_blobs(files)
        place = {rel: {"blob_id": m["blob_id"]} for rel, m in metas.items()}
        result.update(stored="blobs", blob_expires=min(m["expires"] for m in metas.values()))
        notes.append("Each file's blob id is its sha256, as listed in manifest.json. The Inline urls in scene.x3d are "
                     "relative (chunks/cNNN.x3d): put the files in one folder and serve it, or pass url_prefix. "
                     "validate_x3d and validate_semantic take any one of these ids.")

    digest = {manifest["master"]["file"]: manifest["master"]["sha256"], **{c["file"]: c["sha256"] for c in manifest["chunks"]}}

    def entry(rel: str, **extra) -> dict:
        sha = digest.get(rel) or hashlib.sha256(files[rel]).hexdigest()
        return {"file": rel, "bytes": len(files[rel]), "sha256": sha, **extra, **place[rel]}

    result["manifest"] = entry("manifest.json")
    result["master"] = entry("scene.x3d")
    if opts["bundle"]:
        result["bundle"] = entry("bundle.tar")
    result.update(cap_items([entry(c["file"], id=c["id"], splats=c["splats"]) for c in manifest["chunks"]], "chunk_files"))
    if notes:
        result["notes"] = notes
    return result


# ---------------------------------------------------------------- tools

def register(mcp: FastMCP):

    @mcp.tool(structured_output=False)
    async def splat_inspect(path: str = "", blob_id: str = "", ctx: Context = None) -> str:
        """Report on a Gaussian splat file: count, SH degree, bounding box, statistics, memory estimates and a verdict.

        Reads an SPZ file (versions 2, 3 and 4), a 3DGS PLY, or an X3D text scene with GaussianSplats nodes,
        on the server, without building any XML: 500,000 splats take about half a second. The model gets a
        report of a few KB, not the file. The verdict is ok, suspect or unusable with a reason per finding
        (logit opacities, log scales, NaN, far floaters, huge or needle splats, a quaternion written w x y z, ...);
        thresholds are in `limits`. `x3d_text` says how big the scene is as X3D text and how many chunks
        `splat_chunk` would need for each node to stay under libxml2's 10 MB start-tag limit.

        Args:
            path: A local splat file (stdio transport only).
            blob_id: Id of a stored blob holding the file (upload it with PUT /blob, or blob_put(path=...)).
                  Provide exactly one of `path` or `blob_id`.
        """
        try:
            data, label = await resolve_bytes_input(path, blob_id, ctx)
            report = await run_heavy(_inspect_job, data, SplatLimits.from_env(), size=_always())
        except ImportError:
            return _err(_NO_NUMPY)
        except MemoryError:
            return _err("out of memory reading this file; lower X3D_MCP_SPLAT_MAX_POINTS or use a smaller file")
        except ValueError as exc:
            return _err(exc)
        return json.dumps(_finite({"source": label, **report}), indent=2)

    @mcp.tool(structured_output=False)
    async def splat_chunk(path: str = "", blob_id: str = "", target: int = 50000, method: str = "kd",
                          precision: str = "auto", coordinate_system: str = "as-is", bbox_mode: str = "cull_safe",
                          name: str = "scene", out_dir: str = "", url_prefix: str = "", drop_invalid: bool = False,
                          validate: bool = False, bundle: bool = False, ctx: Context = None) -> str:
        """Cut a Gaussian splat cloud into spatial chunks that X3D tools can validate and a browser can load.

        X3D 4.1 GaussianSplats holds its data in attributes, and libxml2 refuses a start tag over 10 MB,
        so a 500,000-splat scene (49 to 65 MB of text) cannot be validated as one node. This reads an SPZ,
        PLY or X3D text scene on the server and writes an envelope: `scene.x3d` (a master of Inline nodes),
        `chunks/cNNN.x3d` (one GaussianSplats node each, at most `target` splats and under 8 MB of text) and
        `manifest.json` (counts, sizes, sha256 per file, the partition, the written and tight boxes).
        Every file validates with validate_x3d and validate_semantic. Chunks are disjoint blocks of space, so
        the nodes layer in the right order from any viewpoint; cutting in file order is measurably worse.

        By default the files go to the blob store and the answer lists their blob ids (each id is the file's
        sha256). With `out_dir` (stdio only) they are written to a new or empty folder instead. The answer
        never carries file contents.

        Args:
            path: A local splat file (stdio only). Provide exactly one of `path` or `blob_id`.
            blob_id: Id of a stored blob holding the SPZ, PLY or X3D text file.
            target: Most splats per chunk (default 50000). Lowered automatically so a chunk's start tag stays
                  under 8 MB at the chosen digits (about 14,000 splats with SH degree 3).
            method: "kd" (default: median cuts, exactly ceil(n/target) near-equal chunks), "octree" or "grid".
            precision: "auto" (default: digits derived from the SPZ quantisation steps for SPZ, six decimals
                  otherwise), "lattice" (SPZ only), "bridge" (six decimals) or "exact" (nine significant digits).
            coordinate_system: "as-is" (default), "rub" (the same) or "rdf": wraps the chunks in a Transform that
                  turns the scene half a turn about X, for y-down files (a 3DGS training PLY is right-down-forward).
            bbox_mode: "cull_safe" (default: each node's box is centred on its own splats and sized to cover the
                  whole scene, so a viewer never culls a node) or "tight" (the chunk's real box).
            name: Letters, digits, "-" and "_"; names the scene in the master's title.
            out_dir: Write the files here instead of the blob store (stdio only; must be new or empty).
            url_prefix: Prepended to the Inline urls, for files hosted at an absolute location.
            drop_invalid: Leave out splats holding NaN or infinity (default: fail and say how many).
            validate: Also run the XSD and semantic checks on every file and report the verdicts (about 10 s
                  for 500,000 splats).
            bundle: Also store a deterministic tar of the whole envelope (one extra file/blob).
        """
        out_dir = (out_dir or "").strip()
        try:
            cfg = get_config()
            if out_dir:
                if cfg.http or transport_is_http():
                    raise ValueError("out_dir is disabled over the HTTP transport. Leave it empty to store the "
                                     "files in the blob store, then download them from GET /blob/<id>.")
                _check_out_dir(out_dir)
            else:
                denied = tool_gate(ctx, cfg)
                if denied:
                    raise ValueError(denied + ("; or pass out_dir to write the files to a folder (stdio only)"
                                               if not (cfg.http or transport_is_http()) else ""))
            data, _ = await resolve_bytes_input(path, blob_id, ctx)
            opts = dict(target=int(target), method=method, precision=precision, coordinate_system=coordinate_system,
                        bbox_mode=bbox_mode, name=name, out_dir=out_dir, url_prefix=url_prefix,
                        drop_invalid=bool(drop_invalid), validate=bool(validate), bundle=bool(bundle))
            result = await run_heavy(_chunk_job, data, SplatLimits.from_env(), opts, size=_always())
        except ImportError:
            return _err(_NO_NUMPY)
        except MemoryError:
            return _err("out of memory chunking this file; lower X3D_MCP_SPLAT_MAX_POINTS, use a smaller file, or "
                        "raise target so fewer chunks are held at once")
        except ValueError as exc:
            return _err(exc)
        return json.dumps(_finite(result), indent=2)
