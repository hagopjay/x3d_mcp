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

from .blobs import get_config, get_store

MODES = ("full", "summary", "blob")
XML_TYPE = "model/x3d+xml"
CLIP = 300


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
    """Digest and size of the input: the blob id (no rehash) when it came from the store."""
    if blob_id and blob_id.strip():
        meta = get_store().stat(blob_id)
        return {"sha256": meta["sha256"], "bytes": meta["size"]}
    data = text.encode("utf-8")
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


def envelope(mode: str, source: str, **fields) -> str:
    """The JSON a summary or blob return carries."""
    return json.dumps({"return_mode": mode, "source": source, **fields}, indent=2)


def error_envelope(mode: str, message: str, source: str = "") -> str:
    body = {"return_mode": mode, "ok": False, "error": message}
    if source:
        body["source"] = source
    return json.dumps(body, indent=2)
