"""Claim ledger tools: claim_append, claims_page, claims_verify.

A ledger is JSON Lines (see x3d_utils/claims.py). It can be passed inline or named by blob_id; claim_append
with store=True keeps the grown ledger as a new blob and answers with its id, so a long ledger is never pasted
back and forth. Blobs are content addressed, so each append makes a new id; the hash chain inside the ledger
is what ties the versions together. Blobs expire unless the server keeps them (set X3D_MCP_BLOB_DIR to a
private directory to persist them).
"""

import json

from mcp.server.fastmcp import Context, FastMCP

from transfer.errors import BlobError
from transfer.outputs import document_info
from transfer.tooling import require_blob_access, resolve_input
from x3d_utils import claims

JSONL = "application/x-ndjson"


def _err(msg) -> str:
    return json.dumps({"error": str(msg)})


async def _ledger_text(ledger: str, blob_id: str, ctx) -> str:
    """The ledger text; an empty ledger (nothing passed) is a new, empty one."""
    if (blob_id or "").strip():
        text, _ = await resolve_input("", "", blob_id, ctx)
        return text
    return ledger or ""


def register(mcp: FastMCP):

    @mcp.tool(structured_output=False)
    async def claim_append(claim: str, grade: str, subject: str = "", instrument: str = "{}", evidence: str = "",
                           ledger: str = "", blob_id: str = "", chain: bool = True, store: bool = False,
                           ctx: Context = None) -> str:
        """Add one claim to a ledger and return the new record and its hash.

        Args:
            claim: What was found, one plain sentence.
            grade: Confirmed (measured or tested), Likely (follows from what was measured) or Suspected (not checked).
            subject: What the claim is about (a scene, a tool, a file).
            instrument: JSON object naming what produced it, e.g. {"validator": "688174f", "rdflib": "7.6.0"}.
            evidence: Where to look (a path, a test name, a commit).
            ledger: The existing ledger as JSON Lines text (empty to start a new one).
            blob_id: The existing ledger as a stored blob, instead of `ledger`.
            chain: Link the new record to the last one by its hash (default true).
            store: Keep the grown ledger as a blob and return its blob_id instead of the text.

        Returns JSON {record, line, records, head, ledger_blob_id?, ledger?}. `ledger` (the full new text) is
        returned only when store is false and the ledger is under 64 KB.
        """
        try:
            inst = json.loads(instrument) if instrument.strip() else {}
            text = await _ledger_text(ledger, blob_id, ctx)
            new_text, rec = claims.append(text, {"claim": claim, "grade": grade, "subject": subject,
                                                 "instrument": inst, "evidence": evidence}, chain)
            out = {"record": rec, "line": claims.canonical(rec),
                   "records": new_text.count("\n"), "head": rec["sha256"]}
            if store:
                require_blob_access(ctx)
                info = document_info(new_text, JSONL, True, "claims.jsonl")
                out["ledger_blob_id"] = info["blob_id"]
            elif len(new_text) < 65536:
                out["ledger"] = new_text
            return json.dumps(out, indent=2)
        except (claims.ClaimError, ValueError, BlobError, json.JSONDecodeError) as e:
            return _err(e)

    @mcp.tool(structured_output=False)
    async def claims_page(ledger: str = "", blob_id: str = "", offset: int = 0, limit: int = 20,
                          columns: str = "", hash_chars: int = 0, drop_hashes: bool = False,
                          ctx: Context = None) -> str:
        """Read one page of a claim ledger.

        A full record is about 650 bytes and about 65% of that is hashes, so a page of 50 full rows is
        about 27 KB. Use drop_hashes (9 KB) or hash_chars=12 (13 KB) when you only need to read the claims.

        Args:
            ledger: The ledger as JSON Lines text.
            blob_id: The ledger as a stored blob, instead of `ledger`.
            offset: First record of the page (0-based).
            limit: Records per page, 1 to 500 (default 20).
            columns: Comma list from schema, claim, grade, subject, instrument, evidence, prev, sha256 (default all).
            hash_chars: Shorten prev and sha256 to this many characters (0 = full).
            drop_hashes: Leave out prev and sha256 when `columns` is not given.

        Returns JSON {columns, rows, total, offset, next_offset, page_sha256}.
        """
        try:
            text = await _ledger_text(ledger, blob_id, ctx)
            return json.dumps(claims.page(claims.parse_ledger(text), offset, limit, columns,
                                          hash_chars, drop_hashes), separators=(",", ":"))
        except (claims.ClaimError, ValueError, BlobError) as e:
            return _err(e)

    @mcp.tool(structured_output=False)
    async def claims_verify(ledger: str = "", blob_id: str = "", chain: bool = True,
                            ctx: Context = None) -> str:
        """Check every record's hash, and with chain that each record links to the one before.

        Args:
            ledger: The ledger as JSON Lines text.
            blob_id: The ledger as a stored blob, instead of `ledger`.
            chain: Also check the prev links (default true; turn off for a ledger written without chain).

        Returns JSON {records, ok, problems (first 20), head}.
        """
        try:
            text = await _ledger_text(ledger, blob_id, ctx)
            return json.dumps(claims.verify(claims.parse_ledger(text), chain))
        except (claims.ClaimError, ValueError, BlobError) as e:
            return _err(e)
