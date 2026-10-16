"""Tests for the Accountability Playground's pieces that need no web server:
the validator report, access rules, signed passes and cookies, rate limits and
the spend guard, the defect injector, the judge comparison, and the loop
itself driven by a scripted model."""

import asyncio
import sys
import time
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))

from playground import auth, mutate, prompts  # noqa: E402
from playground.config import WRITERS, Limits, Settings, cost_usd, judge_for  # noqa: E402
from playground.judge import compare  # noqa: E402
from playground.limits import Denied, Guard, effective_limits  # noqa: E402
from playground.llm import Usage  # noqa: E402
from playground.loop import RunRequest, extract_x3d, run_loop  # noqa: E402
from playground.report import apply_auto_repair, defects_for_prompt, full_report  # noqa: E402
from playground.store import DEFAULT_ACCESS, MemoryStore  # noqa: E402

VALID = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Interchange" version="4.0" xmlns:xsd="http://www.w3.org/2001/XMLSchema-instance" xsd:noNamespaceSchemaLocation="https://www.web3d.org/specifications/x3d-4.0.xsd">
  <head><meta name="title" content="Test box"/></head>
  <Scene>
    <Viewpoint DEF="Front" description="Front view" position="0 0 6"/>
    <Shape><Appearance><Material diffuseColor="1 0 0"/></Appearance><Box/></Shape>
  </Scene>
</X3D>"""
BAD_ATTR = VALID.replace('diffuseColor="1 0 0"', 'diffuseColour="1 0 0"')

ANIMATED = """<?xml version="1.0" encoding="UTF-8"?>
<X3D profile="Immersive" version="4.0" xmlns:xsd="http://www.w3.org/2001/XMLSchema-instance" xsd:noNamespaceSchemaLocation="https://www.web3d.org/specifications/x3d-4.0.xsd">
  <head><meta name="title" content="Spinner"/></head>
  <Scene>
    <Viewpoint DEF="Front" description="Front view" position="0 0 6"/>
    <Transform DEF="Spinner">
      <TouchSensor DEF="Touch" description="click me"/><Shape><Appearance><Material DEF="Paint" diffuseColor="0.2 0.4 1"/></Appearance><Box/></Shape>
    </Transform>
    <TimeSensor DEF="Clock" cycleInterval="4" loop="true"/>
    <OrientationInterpolator DEF="Turn" key="0 0.5 1" keyValue="0 1 0 0 0 1 0 3.14159 0 1 0 6.28318"/>
    <ROUTE fromNode="Clock" fromField="fraction_changed" toNode="Turn" toField="set_fraction"/>
    <ROUTE fromNode="Turn" fromField="value_changed" toNode="Spinner" toField="set_rotation"/>
  </Scene>
</X3D>"""


def run(coro):
    return asyncio.run(coro)


# ---------------------------------------------------------------- the report

def test_report_valid_scene_passes_every_layer():
    r = full_report(VALID)
    assert r["verdict"] == "pass"
    assert r["wellformed"] and r["xsd_valid"]
    assert r["counts"]["errors"] == 0
    assert r["schema_version"] == "4.0"
    assert len(r["sha256"]) == 64


def test_report_names_the_exact_xsd_defect_with_its_line_and_a_hint():
    r = full_report(BAD_ATTR)
    assert r["verdict"] == "fail"
    errors = [f for f in r["findings"] if f["level"] == "error"]
    assert len(errors) == 1
    f = errors[0]
    assert f["layer"] == "xsd" and f["line"] == 6
    assert "diffuseColour" in f["message"]
    assert f["hint"]
    prompt_text = defects_for_prompt(r)
    assert "line 6" in prompt_text and "diffuseColour" in prompt_text


def test_report_malformed_xml_is_an_xml_layer_error():
    r = full_report(VALID.replace("<Box/>", "<Box>"))
    assert r["verdict"] == "fail"
    assert r["counts"]["xml"] == 1 and not r["wellformed"]


def test_report_semantic_route_typo_is_located():
    r = full_report(ANIMATED.replace('toField="set_rotation"', 'toField="set_rotaton"'))
    assert r["verdict"] == "fail"
    sem = [f for f in r["findings"] if f["layer"] == "semantic" and f["level"] == "error"]
    assert sem and "set_rotaton" in sem[0]["message"]
    assert sem[0]["line"] == 12


def test_auto_repair_fixes_a_missing_profile_and_says_so():
    text = VALID.replace(' profile="Interchange"', "")
    assert full_report(text)["verdict"] == "fail"
    fixed = apply_auto_repair(text)
    assert fixed is not None
    new_text, changes = fixed
    assert any(c["kind"] == "profile" for c in changes)
    assert full_report(new_text)["verdict"] == "pass"


def test_auto_repair_leaves_alone_what_it_cannot_fix_exactly():
    assert apply_auto_repair(BAD_ATTR) is None


def test_extract_x3d_drops_fences_and_prose():
    wrapped = f"Here is your scene:\n```xml\n{VALID}\n```\nEnjoy!"
    assert extract_x3d(wrapped) == VALID


# ---------------------------------------------------------------- access rules

ACCESS = {**DEFAULT_ACCESS, "domains": ["vt.edu"], "emails": ["friend@gmail.com"],
          "blocked": ["bad@vt.edu", "spam.example"], "admins": ["owner@example.org"]}


@pytest.mark.parametrize("email,hd,ok,why", [
    ("student@vt.edu", "", True, "domain vt.edu"),
    ("prof@cs.vt.edu", "", True, "domain vt.edu"),
    ("x@notvt.edu", "", False, "not on the list"),
    ("x@vt.edu.evil.com", "", False, "not on the list"),
    ("Friend@Gmail.com", "", True, "address"),
    ("other@gmail.com", "", False, "not on the list"),
    ("bad@vt.edu", "", False, "blocked"),
    ("a@spam.example", "", False, "blocked"),
    ("owner@example.org", "", True, "admin"),
    ("alias@other.org", "vt.edu", True, "domain vt.edu"),
])
def test_check_access(email, hd, ok, why):
    assert auth.check_access(email, hd, ACCESS) == (ok, why)


def test_signer_cookie_round_trip_and_tamper():
    s = auth.Signer("k1")
    c = s.cookie({"id": "p:abc", "kind": "pass"})
    assert s.read_cookie(c)["id"] == "p:abc"
    body, sig = c.rsplit(".", 1)
    assert s.read_cookie(body + "." + sig[:-1] + ("A" if sig[-1] != "A" else "B")) is None
    assert auth.Signer("k2").read_cookie(c) is None
    assert s.read_cookie("") is None and s.read_cookie("nodot") is None


def test_signer_cookie_expires(monkeypatch):
    s = auth.Signer("k1")
    c = s.cookie({"id": "p:abc", "kind": "pass"})
    monkeypatch.setattr(time, "time", lambda: 10**12)
    assert s.read_cookie(c) is None


def test_pass_tokens_cannot_be_forged():
    s = auth.Signer("k1")
    tok = s.pass_token("abc123")
    assert s.read_pass_token(tok) == "abc123"
    assert s.read_pass_token("abc123.forged") is None
    assert s.read_pass_token("other." + tok.split(".", 1)[1]) is None
    assert s.read_pass_token("abc123") is None


def test_ip_key_is_a_keyed_hash():
    s = auth.Signer("k1")
    k = auth.ip_key("203.0.113.9", s)
    assert "203" not in k and len(k) == 16
    assert k == auth.ip_key("203.0.113.9", s) != auth.ip_key("203.0.113.9", auth.Signer("k2"))


# ---------------------------------------------------------------- limits and spend

def _p(pid="p:t", per_hour=None, per_day=None):
    return auth.Principal(id=pid, kind="pass", display="t", per_hour=per_hour, per_day=per_day)


def test_hourly_limit_and_one_run_in_flight():
    async def go():
        store = MemoryStore()
        g = Guard(store, "test")
        lim = Limits(user_per_hour=2)
        lease = await g.admit_run(_p(), "ip1", lim, {})
        with pytest.raises(Denied) as e:
            await g.admit_run(_p(), "ip1", lim, {})
        assert e.value.reason == "inflight"
        await store.release(lease)
        lease = await g.admit_run(_p(), "ip1", lim, {})
        await store.release(lease)
        with pytest.raises(Denied) as e:
            await g.admit_run(_p(), "ip1", lim, {})
        assert e.value.reason == "user_hour" and e.value.retry_after > 0
        # another person on another network is unaffected
        await g.admit_run(_p("p:other"), "ip2", lim, {})
    run(go())


def test_pass_limits_override_the_defaults():
    async def go():
        store = MemoryStore()
        g = Guard(store, "test")
        lease = await g.admit_run(_p(per_hour=1), "ip", Limits(), {})
        await store.release(lease)
        with pytest.raises(Denied):
            await g.admit_run(_p(per_hour=1), "ip", Limits(), {})
    run(go())


def test_ip_limit_spans_people():
    async def go():
        store = MemoryStore()
        g = Guard(store, "test")
        lim = Limits(ip_per_hour=2)
        for who in ("p:a", "p:b"):
            await store.release(await g.admit_run(_p(who), "same-ip", lim, {}))
        with pytest.raises(Denied) as e:
            await g.admit_run(_p("p:c"), "same-ip", lim, {})
        assert e.value.reason == "ip_hour"
    run(go())


def test_spend_caps_stop_new_runs_and_running_loops():
    async def go():
        store = MemoryStore()
        g = Guard(store, "test")
        lim = Limits(usd_phase=0.05, usd_per_day=1.0)
        assert await g.charge(0.03, lim) is True
        assert await g.charge(0.03, lim) is False      # the loop stops between steps
        with pytest.raises(Denied) as e:
            await g.admit_run(_p(), "ip", lim, {})
        assert e.value.reason == "usd_phase"
    run(go())


def test_pause_switch_and_admin_overrides():
    async def go():
        g = Guard(MemoryStore(), "test")
        with pytest.raises(Denied) as e:
            await g.admit_run(_p(), "ip", Limits(), {"paused": True})
        assert e.value.reason == "paused"
    run(go())
    lim = effective_limits(Limits(), {"limits": {"user_per_hour": 3, "usd_phase": 2.5, "bogus": 1,
                                                 "user_per_day": True}})
    assert lim.user_per_hour == 3 and lim.usd_phase == 2.5 and lim.user_per_day == Limits().user_per_day


def test_validation_has_its_own_hourly_limit():
    async def go():
        g = Guard(MemoryStore(), "test")
        lim = Limits(validate_per_hour=2)
        await g.admit_validate(_p(), "ip", lim)
        await g.admit_validate(_p(), "ip", lim)
        with pytest.raises(Denied):
            await g.admit_validate(_p(), "ip", lim)
    run(go())


def test_costs_round_up_and_the_judge_is_another_family():
    w = WRITERS["a"]
    assert cost_usd(w, 1_000_000, 0) == pytest.approx(w.usd_in)
    for code, w in WRITERS.items():
        j = judge_for(code, "")
        if j is not None:
            assert j.family != w.family


# ---------------------------------------------------------------- defect injector

@pytest.mark.parametrize("op", [m["op"] for m in mutate.catalogue()])
def test_every_mutation_is_caught_by_the_layer_it_claims(op):
    out = mutate.mutate(ANIMATED, op, seed=1)
    assert out["applied"], out
    report = full_report(out["x3d"])
    verdict = report["verdict"]
    if out["caught_by"] == "validator":
        assert verdict == "fail", (op, out["what"])
    elif out["caught_by"] == "warning":
        assert verdict == "pass" and report["counts"]["warnings"] > 0, (op, out["what"])
    else:
        assert verdict == "pass", (op, out["what"])   # only the browser can tell


def test_mutate_rejects_unknown_ops_and_broken_input():
    with pytest.raises(ValueError):
        mutate.mutate(VALID, "no-such-op")
    with pytest.raises(ValueError):
        mutate.mutate("<X3D", "drop-def")


# ---------------------------------------------------------------- the judge comparison

def test_compare_matches_by_line_or_quoted_token():
    report = full_report(BAD_ATTR)
    hit = compare({"verdict": "invalid", "defects": [
        {"line": 7, "what": "unknown attribute on Material", "fix": "rename"},
        {"line": 2, "what": "the head has no description meta", "fix": "add one"}]}, report)
    assert hit["agree"] is True and hit["caught"] == 1 and hit["missed"] == 0
    assert hit["unmatched_claims"] == 1
    by_token = compare({"verdict": "invalid", "defects": [{"what": "diffusecolour is misspelled"}]}, report)
    assert by_token["caught"] == 1
    wrong = compare({"verdict": "valid", "defects": []}, report)
    assert wrong["agree"] is False and wrong["missed"] == 1
    odd = compare({"verdict": "maybe", "defects": "none"}, report)
    assert odd["agree"] is None and odd["judge_defects"] == 0


# ---------------------------------------------------------------- the loop

class ScriptedLLM:
    """Stands in for the model endpoints: each writer call takes the next
    scripted answer; the judge always returns ``judge_answer``."""

    def __init__(self, answers, judge_answer='{"verdict": "valid", "defects": []}', fail_on=None):
        self.answers = list(answers)
        self.judge_answer = judge_answer
        self.writer_turns = []
        self.fail_on = fail_on

    async def stream(self, w, system, turns, *, usage=None, **kw):
        self.writer_turns.append(turns)
        if self.fail_on is not None and len(self.writer_turns) == self.fail_on:
            raise RuntimeError("writer returned HTTP 503")
        text = self.answers.pop(0)
        if usage is not None:
            usage.tokens_in, usage.tokens_out, usage.ms = 400, 900, 12
        for i in range(0, len(text), 64):
            yield text[i:i + 64]

    async def complete(self, w, system, turns, **kw):
        return self.judge_answer, Usage(tokens_in=500, tokens_out=80, ms=9)


def _events(req, llm, charge_ok=True, settings=None):
    settings = settings or Settings()

    async def charge(usd):
        return charge_ok

    async def go():
        holder = {}
        evs = [e async for e in run_loop(req, llm, settings, charge, holder=holder)]
        return evs, holder["record"]
    return run(go())


def test_loop_write_fail_then_repair_from_the_exact_report():
    llm = ScriptedLLM([f"```xml\n{BAD_ATTR}\n```", VALID])
    evs, rec = _events(RunRequest(prompt="a red box", writer="a", autofix=False), llm)
    kinds = [e["t"] for e in evs]
    assert kinds[0] == "run" and "final" in kinds and kinds[-1] == "_record"
    reports = [e["report"]["verdict"] for e in evs if e["t"] == "report"]
    assert reports == ["fail", "pass"]
    final = next(e for e in evs if e["t"] == "final")
    assert final["verdict"] == "pass" and final["x3d"] == VALID
    # the repair turn carries the validator's exact words, and only the last draft
    repair_turns = llm.writer_turns[1]
    assert len(repair_turns) == 3
    assert "diffuseColour" in repair_turns[2]["text"] and "line 6" in repair_turns[2]["text"]
    assert rec.first_verdict == "fail" and rec.final_verdict == "pass"
    s = rec.summary()
    assert s["model_repairs"] == 1 and s["validator_repairs"] == 0
    assert s["judge"]["compare"]["agree"] is False      # the judge said valid; it was not
    assert s["usd"] > 0


def test_loop_validator_fix_comes_before_the_model():
    no_profile = VALID.replace(' profile="Interchange"', "")
    llm = ScriptedLLM([no_profile])
    evs, rec = _events(RunRequest(prompt="a red box", writer="a", judge="off"), llm)
    steps = [(s.kind, s.by, s.verdict) for s in rec.steps]
    assert steps == [("write", "a", "fail"), ("autofix", "validator", "pass")]
    assert len(llm.writer_turns) == 1


def test_loop_paste_mode_calls_no_model_when_the_scene_passes():
    llm = ScriptedLLM([])
    evs, rec = _events(RunRequest(source_x3d=VALID, writer="a", judge="off"), llm)
    assert rec.final_verdict == "pass" and llm.writer_turns == []
    assert rec.steps[0].by == "person"


def test_loop_stops_at_the_repair_limit():
    llm = ScriptedLLM([BAD_ATTR, BAD_ATTR, BAD_ATTR, BAD_ATTR])
    evs, rec = _events(RunRequest(prompt="x", writer="a", judge="off", max_repairs=2, autofix=False), llm)
    assert rec.final_verdict == "fail"
    assert sum(1 for s in rec.steps if s.kind == "repair") == 2


def test_loop_stops_when_the_spend_guard_says_so():
    llm = ScriptedLLM([BAD_ATTR, VALID])
    evs, rec = _events(RunRequest(prompt="x", writer="a", judge="off", autofix=False), llm, charge_ok=False)
    assert rec.stopped == "spend cap reached"
    assert len(llm.writer_turns) == 1


def test_loop_reports_a_writer_error_and_still_ends_cleanly():
    llm = ScriptedLLM([BAD_ATTR], fail_on=2)
    evs, rec = _events(RunRequest(prompt="x", writer="a", judge="off", autofix=False), llm)
    assert any(e["t"] == "stage" and e.get("state") == "error" for e in evs)
    assert rec.stopped == "writer error" and rec.final_verdict == "fail"
    assert [e["t"] for e in evs].count("final") == 1


def test_loop_visibility_repair_sends_the_browsers_report():
    llm = ScriptedLLM([VALID])
    vis = {"status": "blank", "coverage": 0.0, "background": [13, 15, 25], "width": 400, "height": 225,
           "messages": ["warn: something odd"]}
    evs, rec = _events(RunRequest(prompt="x", writer="a", source_x3d=VALID, visibility=vis), llm)
    assert rec.mode == "visible-repair" and rec.judge == ""
    text = llm.writer_turns[0][2]["text"]
    assert "blank" in text and "something odd" in text


def test_loop_pings_while_a_model_is_silent():
    class Slow(ScriptedLLM):
        async def stream(self, w, system, turns, *, usage=None, **kw):
            await asyncio.sleep(0.25)
            async for piece in super().stream(w, system, turns, usage=usage, **kw):
                yield piece

    async def charge(usd):
        return True

    async def go():
        return [e async for e in run_loop(RunRequest(prompt="x", writer="a", judge="off"), Slow([VALID]),
                                          Settings(), charge, ping_s=0.05)]
    evs = run(go())
    assert any(e["t"] == "_ping" for e in evs)
    assert next(e for e in evs if e["t"] == "final")["verdict"] == "pass"


def test_closing_the_stream_early_cancels_the_run_and_keeps_the_record():
    class Hang(ScriptedLLM):
        async def stream(self, w, system, turns, *, usage=None, **kw):
            await asyncio.sleep(30)
            yield ""

    async def charge(usd):
        return True

    async def go():
        holder = {}
        gen = run_loop(RunRequest(prompt="x", writer="a", judge="off"), Hang([]), Settings(), charge,
                       holder=holder, ping_s=0.05)
        async for e in gen:
            if e["t"] == "_ping":
                break
        await gen.aclose()
        return holder["record"]
    t0 = time.time()
    rec = run(go())
    assert time.time() - t0 < 5
    assert rec.stopped == "stopped before the end"


def test_prompts_name_the_version_rules():
    assert "4.1" in prompts.write_system("4.1")
    assert prompts.numbered("a\nb").splitlines()[1].strip().startswith("2")


# ---------------------------------------------------------------- the journaled store

class FakeRunLog:
    """Keeps inserted rows and answers JournalStore.load's four queries the
    way BigQuery would, so a restart can be replayed in a test."""

    enabled = True

    def __init__(self):
        self.rows = []

    def fq_table(self):
        return "`p.d.t`"

    async def insert(self, row):
        self.rows.append(dict(row))

    async def query(self, sql, params=None):
        state = [r for r in self.rows if r.get("row_kind") == "state"]
        if "state_kind IN" in sql:
            latest = {}
            for r in state:
                if r["state_kind"] in ("access", "pass", "user"):
                    latest[(r["state_kind"], r["state_key"])] = r
            return [{"state_kind": k[0], "state_key": k[1], "payload": r["payload"]} for k, r in latest.items()]
        if "state_kind = 'run'" in sql:
            latest = {}
            for r in state:
                if r["state_kind"] == "run":
                    latest[r["state_key"]] = r
            return [{"payload": r["payload"]} for r in reversed(list(latest.values()))]
        if "state_kind = 'event'" in sql:
            return [{"payload": r["payload"]} for r in reversed(state) if r["state_kind"] == "event"]
        if "row_kind = 'run'" in sql:
            import calendar
            out = []
            for r in self.rows:
                if r.get("row_kind") == "run":
                    t = calendar.timegm(time.strptime(r["ts"][:19], "%Y-%m-%dT%H:%M:%S"))
                    out.append({"principal_hash": r["principal_hash"], "t": str(t),
                                "usd_est": str(r["usd_est"]), "phase": r["phase"]})
            return out
        raise AssertionError(sql)


def test_journal_store_survives_a_restart_with_spend_and_counts():
    from playground.runlog import row_for
    from playground.store import JournalStore

    async def go():
        log = FakeRunLog()
        a = JournalStore(log, "phase-x")
        await a.load()
        await a.put_access({**DEFAULT_ACCESS, "domains": ["vt.edu"]}, "owner")
        await a.put_pass("pid1", {"label": "Guest", "revoked": False, "created": 1})
        await a.update_pass("pid1", {"revoked": True})
        await a.touch_user("p:pid1", {"display": "Guest"}, runs=1)
        await a.add_event("signin", {"who": "Guest"})
        # a finished run, as app._finish logs it
        g = Guard(a, "phase-x")
        await store_release(a, await g.admit_run(_p("p:pid1"), "ip", Limits(), {}))
        holder = {}

        async def ok(usd):
            return True
        _ = [e async for e in run_loop(RunRequest(prompt="x", writer="a", judge="off"), ScriptedLLM([VALID]),
                                       Settings(), ok, holder=holder)]
        rec = holder["record"]
        rec.usd = 2.5
        await a.put_run(rec.run_id, {"run_id": rec.run_id, "ts": rec.started, "principal": "p:pid1"})
        await a.update_run(rec.run_id, {"visible": {"status": "ok"}})
        log.rows.append(row_for(rec, _p("p:pid1"), WRITERS["a"], None, "phase-x"))
        assert all(r["row_kind"] == "state" for r in log.rows[:-1])

        b = JournalStore(log, "phase-x")      # the restart
        loaded = await b.load()
        assert loaded["passes"] == 1 and loaded["runs"] == 1 and loaded["events"] == 1
        assert (await b.get_access())["domains"] == ["vt.edu"]
        assert (await b.get_pass("pid1"))["revoked"] is True
        assert (await b.get_run(rec.run_id))["visible"]["status"] == "ok"
        snap = await Guard(b, "phase-x").snapshot(_p("p:pid1"), "ip", Limits())
        assert snap["usd_phase"] == pytest.approx(2.5) and snap["usd_day"] == pytest.approx(2.5)
        assert snap["runs_hour"] == 1 and snap["runs_day"] == 1 and snap["global_day"] == 1
        # a cap reached before the restart still holds after it
        with pytest.raises(Denied) as e:
            await Guard(b, "phase-x").admit_run(_p("p:new"), "ip2", Limits(usd_phase=2.0), {})
        assert e.value.reason == "usd_phase"

    async def store_release(store, lease):
        await store.release(lease)

    asyncio.run(go())
