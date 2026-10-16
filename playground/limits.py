"""Rate limits and the spend guard.

Checked before every model run, in this order, so the message names the
tightest limit: the global pause switch, the phase and daily spend caps, the
global daily run count, the person's hourly and daily runs, the address's
hourly runs, and one run in flight per person.

Spend is estimated from token counts and config prices (rounded up), charged
as each model call ends, and the run stops between steps once a cap is hit.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from playground.auth import Principal
from playground.config import Limits


@dataclass
class Denied(Exception):
    reason: str
    message: str
    retry_after: int = 0


def _hour(t: float) -> str:
    return time.strftime("%Y%m%d%H", time.gmtime(t))


def _day(t: float) -> str:
    return time.strftime("%Y%m%d", time.gmtime(t))


def effective_limits(base: Limits, access: dict) -> Limits:
    over = access.get("limits") or {}
    data = base.as_dict()
    for k, v in over.items():
        if k in data and isinstance(v, (int, float)) and not isinstance(v, bool):
            data[k] = type(data[k])(v)
    return Limits(**data)


class Guard:
    def __init__(self, store, phase: str):
        self.store = store
        self.phase = phase

    def keys(self, p: Principal, ipk: str, now: float | None = None) -> dict:
        now = now or time.time()
        h, d = _hour(now), _day(now)
        return {"u_h": f"u-{p.id}-h-{h}", "u_d": f"u-{p.id}-d-{d}", "ip_h": f"ip-{ipk}-h-{h}",
                "g_d": f"g-runs-d-{d}", "usd_d": f"g-usd-d-{d}", "usd_p": f"g-usd-p-{self.phase}",
                "v_h": f"v-{p.id}-h-{h}"}

    async def snapshot(self, p: Principal, ipk: str, lim: Limits) -> dict:
        k = self.keys(p, ipk)
        c = await self.store.get_counters(list(k.values()))
        per_hour = p.per_hour if p.per_hour is not None else lim.user_per_hour
        per_day = p.per_day if p.per_day is not None else lim.user_per_day
        return {
            "runs_hour": int(c[k["u_h"]]), "runs_hour_limit": per_hour,
            "runs_day": int(c[k["u_d"]]), "runs_day_limit": per_day,
            "ip_hour": int(c[k["ip_h"]]), "ip_hour_limit": lim.ip_per_hour,
            "global_day": int(c[k["g_d"]]), "global_day_limit": lim.global_runs_per_day,
            "usd_day": round(c[k["usd_d"]], 4), "usd_day_limit": lim.usd_per_day,
            "usd_phase": round(c[k["usd_p"]], 4), "usd_phase_limit": lim.usd_phase,
            "validate_hour": int(c[k["v_h"]]), "validate_hour_limit": lim.validate_per_hour,
        }

    async def admit_run(self, p: Principal, ipk: str, lim: Limits, access: dict) -> str:
        """Raise Denied, or reserve one run and return the in-flight lease key."""
        if access.get("paused"):
            raise Denied("paused", "The playground is paused by its owner. Validation still works.")
        s = await self.snapshot(p, ipk, lim)
        now = time.time()
        to_hour = 3600 - int(now % 3600)
        to_day = 86400 - int(now % 86400)
        if s["usd_phase"] >= s["usd_phase_limit"]:
            raise Denied("usd_phase", "This phase's model budget is used up. Validation still works.")
        if s["usd_day"] >= s["usd_day_limit"]:
            raise Denied("usd_day", "Today's model budget is used up. It resets at 00:00 UTC.", to_day)
        if s["global_day"] >= s["global_day_limit"]:
            raise Denied("global_day", "The playground has reached today's run limit.", to_day)
        if s["runs_hour"] >= s["runs_hour_limit"]:
            raise Denied("user_hour", f"You have used this hour's {s['runs_hour_limit']} runs.", to_hour)
        if s["runs_day"] >= s["runs_day_limit"]:
            raise Denied("user_day", f"You have used today's {s['runs_day_limit']} runs.", to_day)
        if s["ip_hour"] >= s["ip_hour_limit"]:
            raise Denied("ip_hour", "Too many runs from this network this hour.", to_hour)
        lease = f"run-{p.id}"
        if not await self.store.acquire(lease, lim.run_timeout_s + 30):
            raise Denied("inflight", "One run at a time: your previous run is still going.", 10)
        k = self.keys(p, ipk)
        await self.store.bump({k["u_h"]: 1, k["ip_h"]: 1}, 3700)
        await self.store.bump({k["u_d"]: 1, k["g_d"]: 1}, 90000)
        return lease

    async def admit_validate(self, p: Principal, ipk: str, lim: Limits) -> None:
        k = self.keys(p, ipk)
        c = await self.store.get_counters([k["v_h"]])
        if c[k["v_h"]] >= lim.validate_per_hour:
            raise Denied("validate_hour", f"You have used this hour's {lim.validate_per_hour} validations.",
                         3600 - int(time.time() % 3600))
        await self.store.bump({k["v_h"]: 1}, 3700)

    async def charge(self, usd: float, lim: Limits) -> bool:
        """Record spend; False once a cap is reached, so the loop stops between steps."""
        if usd > 0:
            k = self.keys(Principal("-", "-", "-"), "-")
            await self.store.bump({k["usd_d"]: usd}, 90000)
            await self.store.bump({k["usd_p"]: usd}, 120 * 86400)
        k = self.keys(Principal("-", "-", "-"), "-")
        c = await self.store.get_counters([k["usd_d"], k["usd_p"]])
        return c[k["usd_d"]] < lim.usd_per_day and c[k["usd_p"]] < lim.usd_phase
