"""Return modes for tools that produce a document: full, summary, blob.

  full     the tool's output exactly as it was before this module existed (the default)
  summary  JSON: digest, byte size, node counts and the list of changes, not the document
  blob     the same JSON plus blob_id: the document is stored and fetched by id
           (blob_read pages it, a later tool call takes it as blob_id)

A 10 MB scene edited ten times moves 218 MB through the model today; with ids and
summaries it moves a few kilobytes. Lists in a summary are capped (X3D_MCP_SUMMARY_ITEMS)
and long strings clipped, with the true totals stated.
"""

import hashlib
import io
import json
from collections import Counter

from lxml import etree

from .blobs import get_config, get_store, normalize_blob_id
from .errors import BlobError

MODES = ("full", "summary", "blob")
XML_TYPE = "model/x3d+xml"
CLIP = 300
ERROR_CLIP = 1000        # an error message in a summary or blob answer
SOURCE_CLIP = 200        # the "source" label: a path or other text the caller supplied


def parse_mode(value: str | None) -> str:
    mode = (value or "full").strip().lower() or "full"
    if mode not in MODES:
        raise ValueError(f"return_mode must be one of {', '.join(MODES)} (got {value!r})")
    return mode


def clip(value, n: int = CLIP):
    """Clip a long string, saying how much was dropped; other values pass through."""
    if isinstance(value, str) and len(value) > n:
        return value[:n] + f"... [{len(value) - n} more characters]"
    return value


def clip_deep(obj, n: int = CLIP):
    if isinstance(obj, dict):
        return {k: clip_deep(v, n) for k, v in obj.items()}
    if isinstance(obj, list):
        return [clip_deep(v, n) for v in obj]
    return clip(obj, n)


def cap_items(items: list, key: str, limit: int | None = None) -> dict:
    """{key: first `limit` items, key_total: n, key_truncated: True (only when cut)}."""
    limit = get_config().summary_items if limit is None else limit
    items = list(items)
    out = {key: items[:limit], f"{key}_total": len(items)}
    if len(items) > limit:
        out[f"{key}_truncated"] = True
    return out


def count_nodes(data: bytes, top: int = 12) -> dict | None:
    """{"nodes": total elements, "node_types": the `top` commonest tags} for XML, else None."""
    if not data.lstrip()[:1] == b"<":
        return None
    counts: Counter = Counter()
    try:
        ctx = etree.iterparse(io.BytesIO(data), events=("start", "end"), huge_tree=True,
                              resolve_entities=False, no_network=True)
        for event, el in ctx:
            if event == "start":
                tag = el.tag if isinstance(el.tag, str) else ""
                counts[tag.rsplit("}", 1)[-1]] += 1
            else:
                el.clear()
                while el.getprevious() is not None and el.getparent() is not None:
                    del el.getparent()[0]
    except etree.XMLSyntaxError:
        return None
    counts.pop("", None)
    return {"nodes": sum(counts.values()), "node_types": dict(counts.most_common(top))}


def input_info(text: str, blob_id: str = "") -> dict:
    """Digest and size of the input. For a blob the id is the digest (the text was decoded
    from the bytes it names), so there is no rehash and no store lookup: a blob that expired
    or was deleted while the job ran cannot turn a finished result into an error."""
    if blob_id and blob_id.strip():
        return {"sha256": normalize_blob_id(blob_id),
                "bytes": len(text) if text.isascii() else len(text.encode("utf-8", "surrogatepass"))}
    data = text.encode("utf-8", "surrogatepass")
    return {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data)}


def document_info(text: str, media_type: str, store_blob: bool = False, name: str = "") -> dict:
    """Describe a produced document; with store_blob, also keep it and add blob_id and blob_expires."""
    data = text.encode("utf-8")
    part = {"sha256": hashlib.sha256(data).hexdigest(), "bytes": len(data), "media_type": media_type}
    if media_type == XML_TYPE:
        counted = count_nodes(data)
        if counted:
            part.update(counted)
    if store_blob:
        meta = get_store().put(data, media_type=media_type, name=name)
        part["blob_id"] = meta["blob_id"]
        part["stored_size"] = meta["stored_size"]
        part["blob_expires"] = meta["expires"]
    return part


def report_info(text: str, media_type: str) -> dict:
    """Describe a produced report and store it. When the store refuses (full, too many blobs,
    switched off) the description still comes back, with stored false and the reason, so the
    verdict that went with the report is not lost."""
    try:
        return document_info(text, media_type, True)
    except BlobError as e:
        return {**document_info(text, media_type), "stored": False, "error": clip(str(e), ERROR_CLIP)}


def envelope(mode: str, source: str, **fields) -> str:
    """The JSON a summary or blob return carries."""
    return json.dumps({"return_mode": mode, "source": clip(source, SOURCE_CLIP), **fields}, indent=2)


def error_envelope(mode: str, message: str, source: str = "") -> str:
    """A failure as JSON. Message and source are clipped: both can carry text the caller
    supplied (a document mistaken for a path), and these modes exist to keep answers small."""
    body = {"return_mode": mode, "ok": False, "error": clip(message, ERROR_CLIP)}
    if source:
        body["source"] = clip(source, SOURCE_CLIP)
    return json.dumps(body, indent=2)
