"""The accountability loop.

    write  ->  validate  ->  (validator auto-repair)  ->  model repair  ->  validate ...
                                  \\-> judge alone, on the first draft, in parallel

Every step is an event, so the page can draw the loop as it runs and the run
log can say who changed what: the writer, the validator's repair_x3d, or the
person who pasted the scene.
"""

from __future__ import annotations

import asyncio
import re
import time
import uuid
from dataclasses import dataclass, field
from typing import AsyncIterator, Awaitable, Callable

from playground import prompts
from playground.config import WRITERS, Settings, cost_usd, judge_for
from playground.judge import run_judge
from playground.llm import LLM, Usage
from playground.report import apply_auto_repair, defects_for_prompt, full_report, scene_hash

_FENCE_RE = re.compile(r"^```[a-zA-Z]*\s*\n|\n```\s*$")


def extract_x3d(text: str) -> str:
    """The X3D document inside a writer's answer: fences and prose dropped."""
    t = text.strip()
    t = _FENCE_RE.sub("", t).strip()
    start = t.find("<?xml")
    if start < 0 or start > t.find("<X3D") >= 0:
        start = t.find("<X3D")
    if start < 0:
        return t
    end = t.rfind("</X3D>")
    return t[start:end + len("</X3D>")] if end > start else t[start:]


@dataclass
class RunRequest:
    prompt: str = ""
    writer: str = "a"
    judge: str = ""              # "" = automatic, "off" = no judge
    max_repairs: int = 2
    autofix: bool = True
    version: str = "4.0"
    source_x3d: str = ""         # paste mode, or the scene a visibility repair starts from
    visibility: dict | None = None
    parent_run: str = ""


@dataclass
class Step:
    n: int
    kind: str                    # write | source | autofix | repair | visible-repair
    by: str                      # writer code | "validator" | "person"
    sha256: str = ""
    verdict: str = ""
    errors: int = 0
    warnings: int = 0
    xsd: int = 0
    semantic: int = 0
    xml: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    usd: float = 0.0
    ms: int = 0
    validate_ms: float = 0.0
    changes: int = 0


@dataclass
class RunRecord:
    run_id: str
    mode: str
    writer: str
    judge: str
    version: str
    prompt: str
    validator: str
    xite: str
    started: float = field(default_factory=time.time)
    steps: list[Step] = field(default_factory=list)
    final_x3d: str = ""
    final_verdict: str = "fail"
    first_verdict: str = ""
    judge_result: dict | None = None
    usd: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0
    stopped: str = ""            # why the loop ended early, if it did
    parent_run: str = ""

    def summary(self) -> dict:
        return {
            "run_id": self.run_id, "mode": self.mode, "writer": self.writer, "judge_code": self.judge,
            "version": self.version, "validator": self.validator, "xite": self.xite,
            "final_verdict": self.final_verdict, "first_verdict": self.first_verdict,
            "steps": [s.__dict__ for s in self.steps],
            "model_repairs": sum(1 for s in self.steps if s.kind in ("repair", "visible-repair")),
            "validator_repairs": sum(1 for s in self.steps if s.kind == "autofix"),
            "usd": round(self.usd, 5), "tokens_in": self.tokens_in, "tokens_out": self.tokens_out,
            "ms": self.ms, "stopped": self.stopped, "sha256": scene_hash(self.final_x3d) if self.final_x3d else "",
            "judge": self.judge_result, "parent_run": self.parent_run,
        }


Charge = Callable[[float], Awaitable[bool]]   # returns False when the spend guard says stop


async def run_loop(req: RunRequest, llm: LLM | None, settings: Settings,
                   charge: Charge, *, holder: dict | None = None,
                   ping_s: float = 10.0) -> AsyncIterator[dict]:
    """Yield the loop's events. While nothing happens for ``ping_s`` seconds
    (a model thinking before its first token) a ``_ping`` event is yielded, so
    the caller can keep its connection alive and check its deadline without
    cancelling anything. ``holder["record"]`` is the RunRecord from the start,
    so the caller can log a run it stopped early."""
    q: asyncio.Queue = asyncio.Queue()
    w = WRITERS.get(req.writer)
    writer_code = w.code if w else "none"
    judge = None
    if w and req.judge != "off" and not req.visibility:
        judge = judge_for(w.code, req.judge)
    mode = "visible-repair" if req.visibility else ("paste" if req.source_x3d else "prompt")
    rec = RunRecord(run_id=uuid.uuid4().hex[:12], mode=mode, writer=writer_code,
                    judge=judge.code if judge else "", version=req.version, prompt=req.prompt,
                    validator=settings.validator_commit, xite=settings.xite_version,
                    parent_run=req.parent_run)
    if holder is not None:
        holder["record"] = rec

    async def emit(ev: dict):
        await q.put(ev)

    async def validate(n: int, text: str) -> dict:
        report = await asyncio.to_thread(full_report, text)
        await emit({"t": "report", "step": n, "report": report})
        return report

    def record_step(step: Step, report: dict):
        step.sha256 = report["sha256"]
        step.verdict = report["verdict"]
        c = report["counts"]
        step.errors, step.warnings = c["errors"], c["warnings"]
        step.xsd, step.semantic, step.xml = c["xsd"], c["semantic"], c["xml"]
        step.validate_ms = report["ms"]["total"]
        rec.steps.append(step)

    async def write(n: int, kind: str, system: str, turns: list[dict]) -> tuple[str, Usage] | None:
        await emit({"t": "stage", "step": n, "kind": kind, "by": w.code, "state": "start"})
        usage = Usage()
        buf: list[str] = []
        last = time.perf_counter()
        pending: list[str] = []
        try:
            async for piece in llm.stream(w, system, turns, usage=usage):
                buf.append(piece)
                pending.append(piece)
                if time.perf_counter() - last > 0.12:
                    await emit({"t": "delta", "step": n, "text": "".join(pending)})
                    pending.clear()
                    last = time.perf_counter()
        except Exception as exc:
            await emit({"t": "stage", "step": n, "kind": kind, "by": w.code, "state": "error",
                        "message": f"the writer did not answer ({str(exc)[:160]})"})
            return None
        if pending:
            await emit({"t": "delta", "step": n, "text": "".join(pending)})
        usd = cost_usd(w, usage.tokens_in, usage.tokens_out)
        rec.usd += usd
        rec.tokens_in += usage.tokens_in
        rec.tokens_out += usage.tokens_out
        text = extract_x3d("".join(buf))
        if len(text) > settings.limits.max_scene_chars:
            text = text[: settings.limits.max_scene_chars]
        await emit({"t": "draft", "step": n, "kind": kind, "by": w.code, "x3d": text,
                    "tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out,
                    "usd": round(usd, 5), "ms": usage.ms, "finish": usage.finish})
        ok = await charge(usd)
        if not ok:
            rec.stopped = "spend cap reached"
        return text, usage

    async def judge_task(text: str, report: dict):
        await emit({"t": "stage", "step": 0, "kind": "judge", "by": judge.code, "state": "start"})
        try:
            result = await run_judge(llm, judge, text, report)
        except Exception as exc:
            result = {"error": f"the judge did not answer ({str(exc)[:160]})"}
        rec.usd += result.get("usd", 0.0)
        rec.tokens_in += result.get("tokens_in", 0)
        rec.tokens_out += result.get("tokens_out", 0)
        if result.get("usd"):
            await charge(result["usd"])
        rec.judge_result = result
        await emit({"t": "judge", "judge": {"code": judge.code, "label": judge.label}, "result": result})

    async def main():
        t0 = time.perf_counter()
        n = 0
        system = prompts.write_system(req.version)
        judge_job = None
        text = ""
        report = None
        final_sent = False

        async def send_final():
            nonlocal final_sent
            final_sent = True
            rec.final_x3d = text
            rec.final_verdict = report["verdict"] if report else "fail"
            rec.ms = int((time.perf_counter() - t0) * 1000)
            await emit({"t": "final", "x3d": text, "verdict": rec.final_verdict,
                        "summary": rec.summary(), "judge_pending": bool(judge_job and not judge_job.done())})

        try:
            # Step 1: the first draft
            n += 1
            if req.visibility:
                kind = "visible-repair"
                turns = [{"role": "user", "text": req.prompt or "Repair this X3D scene."},
                         {"role": "model", "text": req.source_x3d},
                         {"role": "user", "text": prompts.REPAIR_VISIBLE.format(
                             visibility=_visibility_text(req.visibility))}]
                out = await write(n, kind, system, turns)
                if out is None:
                    rec.stopped = rec.stopped or "writer error"
                    return
                text, usage = out
                step = Step(n, kind, w.code, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                            usd=cost_usd(w, usage.tokens_in, usage.tokens_out), ms=usage.ms)
            elif req.source_x3d:
                kind = "source"
                text = req.source_x3d
                await emit({"t": "draft", "step": n, "kind": kind, "by": "person", "x3d": text})
                step = Step(n, kind, "person")
            else:
                kind = "write"
                out = await write(n, kind, system, [{"role": "user", "text": req.prompt}])
                if out is None:
                    rec.stopped = rec.stopped or "writer error"
                    return
                text, usage = out
                step = Step(n, kind, w.code, tokens_in=usage.tokens_in, tokens_out=usage.tokens_out,
                            usd=cost_usd(w, usage.tokens_in, usage.tokens_out), ms=usage.ms)
            report = await validate(n, text)
            record_step(step, report)
            rec.first_verdict = report["verdict"]
            if judge is not None and llm is not None:
                judge_job = asyncio.create_task(judge_task(text, report))

            repairs = 0
            while report["verdict"] != "pass" and not rec.stopped:
                # The validator's own repairs first: mechanical, exact, free.
                if req.autofix and report.get("auto_repair", {}).get("available"):
                    fixed = await asyncio.to_thread(apply_auto_repair, text)
                    if fixed:
                        n += 1
                        text, changes = fixed
                        await emit({"t": "draft", "step": n, "kind": "autofix", "by": "validator",
                                    "x3d": text, "changes": changes})
                        report = await validate(n, text)
                        record_step(Step(n, "autofix", "validator", changes=len(changes)), report)
                        if report["verdict"] == "pass":
                            break
                if w is None or llm is None or repairs >= max(0, min(req.max_repairs,
                                                                     settings.limits.max_repairs)):
                    break
                repairs += 1
                n += 1
                turns = [{"role": "user", "text": req.prompt or "Repair this X3D scene."},
                         {"role": "model", "text": text},
                         {"role": "user", "text": prompts.REPAIR.format(defects=defects_for_prompt(report))}]
                out = await write(n, "repair", system, turns)
                if out is None:
                    rec.stopped = rec.stopped or "writer error"
                    break
                text, usage = out
                report = await validate(n, text)
                record_step(Step(n, "repair", w.code, tokens_in=usage.tokens_in,
                                 tokens_out=usage.tokens_out,
                                 usd=cost_usd(w, usage.tokens_in, usage.tokens_out), ms=usage.ms), report)
            # The scene is final now: the page can render it while the judge,
            # which never changes the scene, finishes on its own time.
            await send_final()
            if judge_job is not None:
                await judge_job
        except Exception as exc:  # the page must always get a final event
            rec.stopped = "internal error"
            await emit({"t": "error", "message": f"the loop stopped: {type(exc).__name__}: {str(exc)[:200]}"})
        finally:
            if judge_job is not None and not judge_job.done():
                judge_job.cancel()
            if not final_sent:
                await send_final()
            await q.put(None)

    await q.put({"t": "run", "run_id": rec.run_id, "mode": mode,
                 "writer": ({"code": w.code, "label": w.label, "blurb": w.blurb} if w else None),
                 "judge": ({"code": judge.code, "label": judge.label, "blurb": judge.blurb} if judge else None),
                 "validator": settings.validator_commit, "xite": settings.xite_version,
                 "version": req.version, "max_repairs": req.max_repairs, "autofix": req.autofix})
    task = asyncio.create_task(main())
    try:
        while True:
            try:
                # Cancelling a queue read is safe; cancelling this generator is not.
                ev = await asyncio.wait_for(q.get(), timeout=ping_s)
            except asyncio.TimeoutError:
                yield {"t": "_ping"}
                continue
            if ev is None:
                break
            yield ev
        yield {"t": "_record", "record": rec}
    finally:
        if not task.done():
            rec.stopped = rec.stopped or "stopped before the end"
            task.cancel()
            try:
                await task   # lets main() fill in the record's final fields
            except BaseException:
                pass


def _visibility_text(v: dict) -> str:
    lines = []
    status = v.get("status")
    if status:
        pct = float(v.get("coverage", 0.0)) * 100
        lines.append(f"- Frame: {status}, {pct:.3f}% of pixels differ from the background "
                     f"{tuple(v.get('background', []))}.")
    if v.get("bbox"):
        lines.append(f"- Content occupies pixels {v['bbox']} of a {v.get('width')}x{v.get('height')} frame.")
    for m in (v.get("messages") or [])[:12]:
        lines.append(f"- Browser said: {str(m)[:300]}")
    if v.get("load_error"):
        lines.append(f"- The browser could not load the scene: {str(v['load_error'])[:300]}")
    return "\n".join(lines) or "- The frame shows only the background."
