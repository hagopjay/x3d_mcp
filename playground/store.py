"""Where the playground keeps state that must outlive one Cloud Run instance:
the access list, passes, rate counters, the abuse log and recent runs.

Three backends with one interface: ``MemoryStore`` for tests and a laptop,
``JournalStore`` (memory, journaled to the BigQuery run log, one instance) for
Cloud Run today, and ``FirestoreStore`` for Cloud Run at more than one instance. Counters are read, compared and then
incremented; two requests racing can overshoot a limit by one run, which the
per-person in-flight lease keeps to other people's runs only.
"""

from __future__ import annotations

import copy
import time
import uuid
from typing import Any

DEFAULT_ACCESS = {
    "domains": [],        # e.g. "vt.edu" admits any verified address at vt.edu or *.vt.edu
    "emails": [],         # exact addresses
    "blocked": [],        # addresses or domains always refused, checked first
    "admins": [],         # addresses with the admin page, in addition to the env list
    "oauth_client_id": "",
    "limits": {},         # overrides of config.Limits fields
    "writers_off": [],    # writer codes switched off from the admin page
    "paused": False,      # one switch that stops every model call
    "updated": 0,
    "updated_by": "",
}


def _now() -> float:
    return time.time()


class MemoryStore:
    def __init__(self):
        self.access = copy.deepcopy(DEFAULT_ACCESS)
        self.counters: dict[str, tuple[float, float]] = {}
        self.leases: dict[str, float] = {}
        self.events: list[dict] = []
        self.passes: dict[str, dict] = {}
        self.runs: dict[str, dict] = {}
        self.users: dict[str, dict] = {}
        self.kv: dict[str, str] = {}

    async def get_access(self) -> dict:
        return copy.deepcopy(self.access)

    async def put_access(self, access: dict, by: str) -> None:
        self.access = {**copy.deepcopy(DEFAULT_ACCESS), **copy.deepcopy(access),
                       "updated": _now(), "updated_by": by}

    async def get_counters(self, keys: list[str]) -> dict[str, float]:
        now = _now()
        return {k: (v[0] if v[1] > now else 0.0) for k in keys
                for v in [self.counters.get(k, (0.0, 0.0))]}

    async def bump(self, amounts: dict[str, float], ttl_s: int) -> None:
        now = _now()
        for k, a in amounts.items():
            n, exp = self.counters.get(k, (0.0, 0.0))
            if exp <= now:
                n = 0.0
            self.counters[k] = (n + a, max(exp, now + ttl_s))

    async def acquire(self, key: str, seconds: int) -> bool:
        now = _now()
        if self.leases.get(key, 0) > now:
            return False
        self.leases[key] = now + seconds
        return True

    async def release(self, key: str) -> None:
        self.leases.pop(key, None)

    async def add_event(self, kind: str, data: dict) -> None:
        self.events.append({"id": uuid.uuid4().hex[:10], "ts": _now(), "kind": kind, **data})
        del self.events[:-2000]

    async def list_events(self, limit: int = 100) -> list[dict]:
        return list(reversed(self.events[-limit:]))

    async def put_pass(self, pid: str, data: dict) -> None:
        self.passes[pid] = copy.deepcopy(data)

    async def get_pass(self, pid: str) -> dict | None:
        p = self.passes.get(pid)
        return copy.deepcopy(p) if p else None

    async def update_pass(self, pid: str, patch: dict) -> None:
        if pid in self.passes:
            self.passes[pid].update(patch)

    async def list_passes(self) -> list[dict]:
        return sorted((dict(p, id=k) for k, p in self.passes.items()),
                      key=lambda p: -p.get("created", 0))

    async def put_run(self, run_id: str, data: dict) -> None:
        self.runs[run_id] = copy.deepcopy(data)

    async def update_run(self, run_id: str, patch: dict) -> None:
        if run_id in self.runs:
            self.runs[run_id].update(copy.deepcopy(patch))

    async def get_run(self, run_id: str) -> dict | None:
        r = self.runs.get(run_id)
        return copy.deepcopy(r) if r else None

    async def list_runs(self, limit: int = 50, principal: str = "") -> list[dict]:
        rows = [r for r in self.runs.values() if not principal or r.get("principal") == principal]
        rows.sort(key=lambda r: -r.get("ts", 0))
        return copy.deepcopy(rows[:limit])

    async def touch_user(self, uid: str, data: dict, runs: int = 0) -> None:
        u = self.users.setdefault(uid, {"first_seen": _now(), "runs": 0})
        u.update(data)
        u["last_seen"] = _now()
        u["runs"] = u.get("runs", 0) + runs

    async def list_users(self, limit: int = 200) -> list[dict]:
        return sorted((dict(u, id=k) for k, u in self.users.items()),
                      key=lambda u: -u.get("last_seen", 0))[:limit]

    async def get_kv(self, key: str) -> str | None:
        return self.kv.get(key)

    async def put_kv_if_absent(self, key: str, value: str) -> str:
        return self.kv.setdefault(key, value)


class FirestoreStore:
    """Firestore (native mode). Collections: config, counters, leases, events,
    passes, runs, users. Counter and lease documents carry ``exp`` so a TTL
    policy on that field can delete them; nothing depends on that policy."""

    def __init__(self, project: str, database: str):
        from google.cloud import firestore
        self._fs = firestore
        self.db = firestore.AsyncClient(project=project or None, database=database)

    def _c(self, name: str):
        return self.db.collection(name)

    async def get_access(self) -> dict:
        snap = await self._c("config").document("access").get()
        data = snap.to_dict() if snap.exists else {}
        return {**copy.deepcopy(DEFAULT_ACCESS), **(data or {})}

    async def put_access(self, access: dict, by: str) -> None:
        doc = {**copy.deepcopy(DEFAULT_ACCESS), **access, "updated": _now(), "updated_by": by}
        await self._c("config").document("access").set(doc)

    async def get_counters(self, keys: list[str]) -> dict[str, float]:
        refs = [self._c("counters").document(k) for k in keys]
        now = _now()
        out = {k: 0.0 for k in keys}
        async for snap in self.db.get_all(refs):
            if snap.exists:
                d = snap.to_dict()
                if d.get("exp", 0) > now:
                    out[snap.id] = float(d.get("n", 0))
        return out

    async def bump(self, amounts: dict[str, float], ttl_s: int) -> None:
        """Increment counters; a counter past its expiry restarts from the amount."""
        now = _now()
        transaction = self.db.transaction()
        refs = {k: self._c("counters").document(k) for k in amounts}

        @self._fs.async_transactional
        async def txn(tx):
            snaps = {}
            for k, ref in refs.items():
                snaps[k] = await ref.get(transaction=tx)
            for k, ref in refs.items():
                d = snaps[k].to_dict() if snaps[k].exists else {}
                live = d.get("exp", 0) > now
                n = (float(d.get("n", 0)) if live else 0.0) + amounts[k]
                exp = d.get("exp", 0) if live else now + ttl_s
                tx.set(ref, {"n": n, "exp": exp})

        await txn(transaction)

    async def acquire(self, key: str, seconds: int) -> bool:
        ref = self._c("leases").document(key)
        transaction = self.db.transaction()

        @self._fs.async_transactional
        async def txn(tx):
            snap = await ref.get(transaction=tx)
            if snap.exists and snap.to_dict().get("exp", 0) > _now():
                return False
            tx.set(ref, {"exp": _now() + seconds})
            return True

        return await txn(transaction)

    async def release(self, key: str) -> None:
        await self._c("leases").document(key).set({"exp": 0})

    async def add_event(self, kind: str, data: dict) -> None:
        await self._c("events").document().set({"ts": _now(), "kind": kind, **data})

    async def list_events(self, limit: int = 100) -> list[dict]:
        q = self._c("events").order_by("ts", direction=self._fs.Query.DESCENDING).limit(limit)
        return [dict(s.to_dict(), id=s.id) async for s in q.stream()]

    async def put_pass(self, pid: str, data: dict) -> None:
        await self._c("passes").document(pid).set(data)

    async def get_pass(self, pid: str) -> dict | None:
        snap = await self._c("passes").document(pid).get()
        return snap.to_dict() if snap.exists else None

    async def update_pass(self, pid: str, patch: dict) -> None:
        await self._c("passes").document(pid).set(patch, merge=True)

    async def list_passes(self) -> list[dict]:
        q = self._c("passes").order_by("created", direction=self._fs.Query.DESCENDING).limit(500)
        return [dict(s.to_dict(), id=s.id) async for s in q.stream()]

    async def put_run(self, run_id: str, data: dict) -> None:
        await self._c("runs").document(run_id).set(data)

    async def update_run(self, run_id: str, patch: dict) -> None:
        await self._c("runs").document(run_id).set(patch, merge=True)

    async def get_run(self, run_id: str) -> dict | None:
        snap = await self._c("runs").document(run_id).get()
        return snap.to_dict() if snap.exists else None

    async def list_runs(self, limit: int = 50, principal: str = "") -> list[dict]:
        q = self._c("runs")
        if principal:
            q = q.where(filter=self._fs.FieldFilter("principal", "==", principal))
        q = q.order_by("ts", direction=self._fs.Query.DESCENDING).limit(limit)
        return [s.to_dict() async for s in q.stream()]

    async def touch_user(self, uid: str, data: dict, runs: int = 0) -> None:
        patch: dict[str, Any] = {**data, "last_seen": _now()}
        if runs:
            patch["runs"] = self._fs.Increment(runs)
        ref = self._c("users").document(uid)
        snap = await ref.get()
        if not snap.exists:
            patch.setdefault("first_seen", _now())
            if not runs:
                patch["runs"] = 0
        await ref.set(patch, merge=True)

    async def list_users(self, limit: int = 200) -> list[dict]:
        q = self._c("users").order_by("last_seen", direction=self._fs.Query.DESCENDING).limit(limit)
        return [dict(s.to_dict(), id=s.id) async for s in q.stream()]

    async def get_kv(self, key: str) -> str | None:
        snap = await self._c("config").document(f"kv-{key}").get()
        return snap.to_dict().get("v") if snap.exists else None

    async def put_kv_if_absent(self, key: str, value: str) -> str:
        ref = self._c("config").document(f"kv-{key}")
        transaction = self.db.transaction()

        @self._fs.async_transactional
        async def txn(tx):
            snap = await ref.get(transaction=tx)
            if snap.exists:
                return snap.to_dict().get("v")
            tx.set(ref, {"v": value})
            return value

        return await txn(transaction)


class JournalStore(MemoryStore):
    """Memory, journaled to the BigQuery run log and replayed at start.

    Every change to the access list, a pass, a person or a run, and every
    abuse-log event, is appended to the run log table as a ``row_kind='state'``
    row (``state_kind``, ``state_key``, JSON ``payload``); the latest row per key
    wins when the service starts. Spend and run counts are rebuilt from the
    ``row_kind='run'`` rows, so a restart never resets the spend caps.

    One instance only (Cloud Run max-instances=1): leases, the per-network
    counter and the validation counter live in memory and restart at zero.
    Nothing here needs a database; FirestoreStore is the multi-instance path.
    """

    JOURNALED = ("access", "pass", "user", "run", "event")

    def __init__(self, runlog, phase: str):
        super().__init__()
        self.runlog = runlog
        self.phase = phase
        self.loaded: dict = {}

    async def _journal(self, kind: str, key: str, payload: dict) -> None:
        from playground.runlog import state_row
        await self.runlog.insert(state_row(kind, key, payload, self.phase))

    async def put_access(self, access: dict, by: str) -> None:
        await super().put_access(access, by)
        await self._journal("access", "access", self.access)

    async def add_event(self, kind: str, data: dict) -> None:
        await super().add_event(kind, data)
        ev = self.events[-1]
        await self._journal("event", ev["id"], ev)

    async def put_pass(self, pid: str, data: dict) -> None:
        await super().put_pass(pid, data)
        await self._journal("pass", pid, self.passes[pid])

    async def update_pass(self, pid: str, patch: dict) -> None:
        await super().update_pass(pid, patch)
        if pid in self.passes:
            await self._journal("pass", pid, self.passes[pid])

    async def put_run(self, run_id: str, data: dict) -> None:
        await super().put_run(run_id, data)
        await self._journal("run", run_id, self.runs[run_id])

    async def update_run(self, run_id: str, patch: dict) -> None:
        await super().update_run(run_id, patch)
        if run_id in self.runs:
            await self._journal("run", run_id, self.runs[run_id])

    async def touch_user(self, uid: str, data: dict, runs: int = 0) -> None:
        await super().touch_user(uid, data, runs)
        await self._journal("user", uid, self.users[uid])

    async def load(self) -> dict:
        """Replay the journal. Returns what was loaded, for the log."""
        import asyncio
        import json
        q = self.runlog.query
        t = self.runlog.fq_table()
        latest = (f"SELECT state_kind, state_key, payload FROM {t} "
                  f"WHERE row_kind = 'state' AND state_kind IN ('access', 'pass', 'user') "
                  f"QUALIFY ROW_NUMBER() OVER (PARTITION BY state_kind, state_key ORDER BY ts DESC) = 1")
        runs = (f"SELECT payload FROM {t} WHERE row_kind = 'state' AND state_kind = 'run' "
                f"QUALIFY ROW_NUMBER() OVER (PARTITION BY state_key ORDER BY ts DESC) = 1 "
                f"ORDER BY ts DESC LIMIT 400")
        events = (f"SELECT payload FROM {t} WHERE row_kind = 'state' AND state_kind = 'event' "
                  f"ORDER BY ts DESC LIMIT 2000")
        spend = (f"SELECT principal_hash, UNIX_SECONDS(ts) AS t, usd_est, phase FROM {t} "
                 f"WHERE row_kind = 'run' AND (phase = @phase OR ts >= TIMESTAMP_SUB(CURRENT_TIMESTAMP(), "
                 f"INTERVAL 1 DAY))")
        param = [{"name": "phase", "parameterType": {"type": "STRING"},
                  "parameterValue": {"value": self.phase}}]
        r_latest, r_runs, r_events, r_spend = await asyncio.gather(
            q(latest), q(runs), q(events), q(spend, param))
        for row in r_latest:
            data = json.loads(row["payload"] or "{}")
            kind, key = row["state_kind"], row["state_key"]
            if kind == "access":
                self.access = {**copy.deepcopy(DEFAULT_ACCESS), **data}
            elif kind == "pass":
                self.passes[key] = data
            elif kind == "user":
                self.users[key] = data
        for row in r_runs:
            data = json.loads(row["payload"] or "{}")
            if data.get("run_id"):
                self.runs[data["run_id"]] = data
        self.events = [json.loads(row["payload"] or "{}") for row in reversed(r_events)]
        self._rebuild_counters(r_spend)
        self.loaded = {"passes": len(self.passes), "users": len(self.users), "runs": len(self.runs),
                       "events": len(self.events), "run_rows": len(r_spend)}
        return self.loaded

    def _rebuild_counters(self, rows: list[dict]) -> None:
        """The same keys limits.Guard uses, rebuilt from finished runs."""
        now = _now()
        hour = time.strftime("%Y%m%d%H", time.gmtime(now))
        day = time.strftime("%Y%m%d", time.gmtime(now))
        add: dict[str, float] = {}

        def bump(k, v):
            add[k] = add.get(k, 0.0) + v

        for row in rows:
            t = float(row.get("t") or 0)
            usd = float(row.get("usd_est") or 0)
            who = row.get("principal_hash") or "-"
            if row.get("phase") == self.phase:
                bump(f"g-usd-p-{self.phase}", usd)
            if time.strftime("%Y%m%d", time.gmtime(t)) == day:
                bump(f"g-usd-d-{day}", usd)
                bump(f"g-runs-d-{day}", 1)
                bump(f"u-{who}-d-{day}", 1)
            if time.strftime("%Y%m%d%H", time.gmtime(t)) == hour:
                bump(f"u-{who}-h-{hour}", 1)
        for k, v in add.items():
            ttl = 120 * 86400 if k.startswith("g-usd-p-") else (3700 if "-h-" in k else 90000)
            self.counters[k] = (v, now + ttl)


def make_store(settings, runlog=None) -> MemoryStore | FirestoreStore:
    if settings.store == "firestore":
        return FirestoreStore(settings.project, settings.firestore_db)
    if settings.store == "journal":
        if runlog is None or not runlog.enabled:
            raise RuntimeError("PLAYGROUND_STORE=journal needs PLAYGROUND_BQ_TABLE")
        return JournalStore(runlog, settings.phase_label)
    return MemoryStore()
