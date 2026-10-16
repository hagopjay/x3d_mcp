"""Every finished run becomes one BigQuery row, the playground's contribution
to the scene library. Rows carry the writer code and model id (the table is
internal), a keyed hash of the person instead of the address, the prompt,
the per-step verdicts, the judge comparison, the browser's verdict when it
arrives, and the final scene.

Written with the tabledata.insertAll REST call so the image needs no
BigQuery client library. A failed insert is logged and never fails a run.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import httpx

log = logging.getLogger("playground.runlog")

SCHEMA = [
    {"name": "run_id", "type": "STRING", "mode": "REQUIRED"},
    {"name": "ts", "type": "TIMESTAMP", "mode": "REQUIRED"},
    {"name": "phase", "type": "STRING"},
    {"name": "mode", "type": "STRING"},
    {"name": "principal_hash", "type": "STRING"},
    {"name": "principal_kind", "type": "STRING"},
    {"name": "email_domain", "type": "STRING"},
    {"name": "writer", "type": "STRING"},
    {"name": "writer_model", "type": "STRING"},
    {"name": "judge", "type": "STRING"},
    {"name": "judge_model", "type": "STRING"},
    {"name": "x3d_version", "type": "STRING"},
    {"name": "validator_commit", "type": "STRING"},
    {"name": "xite_version", "type": "STRING"},
    {"name": "prompt", "type": "STRING"},
    {"name": "first_verdict", "type": "STRING"},
    {"name": "final_verdict", "type": "STRING"},
    {"name": "model_repairs", "type": "INT64"},
    {"name": "validator_repairs", "type": "INT64"},
    {"name": "steps_json", "type": "STRING"},
    {"name": "judge_json", "type": "STRING"},
    {"name": "judge_agree", "type": "BOOL"},
    {"name": "judge_caught", "type": "INT64"},
    {"name": "judge_missed", "type": "INT64"},
    {"name": "tokens_in", "type": "INT64"},
    {"name": "tokens_out", "type": "INT64"},
    {"name": "usd_est", "type": "FLOAT64"},
    {"name": "ms", "type": "INT64"},
    {"name": "stopped", "type": "STRING"},
    {"name": "final_sha256", "type": "STRING"},
    {"name": "final_x3d", "type": "STRING"},
    {"name": "parent_run", "type": "STRING"},
    # row_kind "visible" rows carry the browser's verdict for the run_id above
    {"name": "row_kind", "type": "STRING"},
    {"name": "visible_status", "type": "STRING"},
    {"name": "coverage", "type": "FLOAT64"},
    {"name": "browser_messages", "type": "STRING"},
    {"name": "browser", "type": "STRING"},
    # row_kind "state" rows journal the service's own state (store.JournalStore)
    {"name": "state_kind", "type": "STRING"},
    {"name": "state_key", "type": "STRING"},
    {"name": "payload", "type": "STRING"},
]


class RunLog:
    def __init__(self, settings, tokens):
        self.table = settings.bq_table          # "dataset.table"
        self.project = settings.project
        self.tokens = tokens
        self.http = httpx.AsyncClient(timeout=20.0)

    @property
    def enabled(self) -> bool:
        return bool(self.table and "." in self.table)

    def _project(self) -> str:
        return self.project or self.tokens.project

    def fq_table(self) -> str:
        dataset, table = self.table.split(".", 1)
        return f"`{self._project()}.{dataset}.{table}`"

    async def query(self, sql: str, params: list[dict] | None = None) -> list[dict]:
        """Standard SQL through the jobs.query REST call; rows as dicts of strings."""
        token = await self.tokens.token()
        base = f"https://bigquery.googleapis.com/bigquery/v2/projects/{self._project()}/queries"
        headers = {"Authorization": f"Bearer {token}"}
        body = {"query": sql, "useLegacySql": False, "timeoutMs": 30000, "maxResults": 5000}
        if params:
            body.update(parameterMode="NAMED", queryParameters=params)
        r = await self.http.post(base, headers=headers, json=body, timeout=60.0)
        d = r.json()
        if r.status_code != 200:
            raise RuntimeError(f"bigquery query failed: {r.status_code} {str(d)[:300]}")
        job = d.get("jobReference", {})
        rows: list[dict] = []
        while True:
            if not d.get("jobComplete"):
                await _sleep(1.0)
            else:
                fields = [f["name"] for f in d.get("schema", {}).get("fields", [])]
                rows += [{fields[i]: c.get("v") for i, c in enumerate(row["f"])} for row in d.get("rows", [])]
                if not d.get("pageToken"):
                    return rows
            params_q = {"location": job.get("location", ""), "timeoutMs": 30000, "maxResults": 5000}
            if d.get("pageToken"):
                params_q["pageToken"] = d["pageToken"]
            r = await self.http.get(f"{base}/{job['jobId']}", headers=headers, params=params_q, timeout=60.0)
            d = r.json()
            if r.status_code != 200:
                raise RuntimeError(f"bigquery results failed: {r.status_code} {str(d)[:300]}")

    async def insert(self, row: dict) -> None:
        if not self.enabled:
            return
        dataset, table = self.table.split(".", 1)
        project = self._project()
        url = (f"https://bigquery.googleapis.com/bigquery/v2/projects/{project}/datasets/"
               f"{dataset}/tables/{table}/insertAll")
        try:
            token = await self.tokens.token()
            r = await self.http.post(url, headers={"Authorization": f"Bearer {token}"},
                                     json={"rows": [{"insertId": row["run_id"] + row.get("_suffix", ""),
                                                     "json": {k: v for k, v in row.items()
                                                              if not k.startswith("_")}}]})
            body = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code != 200 or body.get("insertErrors"):
                log.warning("bigquery insert failed: %s %s", r.status_code, str(body)[:300])
        except Exception as exc:  # never fail a run on logging
            log.warning("bigquery insert error: %s", exc)


async def _sleep(seconds: float) -> None:
    import asyncio
    await asyncio.sleep(seconds)


def _ts(t: float | None = None) -> str:
    t = time.time() if t is None else t
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(t)) + f".{int((t % 1) * 1e6):06d}Z"


def state_row(kind: str, key: str, payload: dict, phase: str) -> dict:
    return {"run_id": f"{kind}:{key}"[:200], "_suffix": "-" + uuid.uuid4().hex[:12], "ts": _ts(),
            "phase": phase, "row_kind": "state", "state_kind": kind, "state_key": key[:200],
            "payload": json.dumps(payload, default=str)[:4_000_000]}


def row_for(record, principal, writer, judge, phase: str) -> dict:
    s = record.summary()
    jc = (s.get("judge") or {}).get("compare") or {}
    return {
        "run_id": record.run_id, "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(record.started)),
        "phase": phase, "mode": record.mode,
        "principal_hash": principal.id, "principal_kind": principal.kind,
        "email_domain": principal.domain or None,
        "writer": record.writer, "writer_model": writer.model if writer else None,
        "judge": record.judge or None, "judge_model": judge.model if judge else None,
        "x3d_version": record.version, "validator_commit": record.validator,
        "xite_version": record.xite, "prompt": record.prompt[:4000],
        "first_verdict": s["first_verdict"], "final_verdict": s["final_verdict"],
        "model_repairs": s["model_repairs"], "validator_repairs": s["validator_repairs"],
        "steps_json": json.dumps(s["steps"]), "judge_json": json.dumps(s.get("judge"))[:20000],
        "judge_agree": jc.get("agree"), "judge_caught": jc.get("caught"), "judge_missed": jc.get("missed"),
        "tokens_in": s["tokens_in"], "tokens_out": s["tokens_out"], "usd_est": s["usd"], "ms": s["ms"],
        "stopped": s["stopped"] or None, "final_sha256": s["sha256"],
        "final_x3d": record.final_x3d[:200_000], "parent_run": record.parent_run or None,
        "row_kind": "run",
    }


def visible_row(run_id: str, phase: str, principal, v: dict) -> dict:
    return {
        "run_id": run_id, "_suffix": "-visible-" + str(v.get("step", "")),
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), "phase": phase,
        "principal_hash": principal.id, "principal_kind": principal.kind,
        "row_kind": "visible", "visible_status": str(v.get("status", ""))[:20],
        "coverage": float(v.get("coverage") or 0.0),
        "browser_messages": json.dumps((v.get("messages") or [])[:20])[:8000],
        "browser": str(v.get("ua", ""))[:300], "final_sha256": str(v.get("sha256", ""))[:64],
        "xite_version": str(v.get("xite", ""))[:20],
    }
