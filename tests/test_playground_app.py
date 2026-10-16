"""HTTP tests for the Accountability Playground service: sign-in by pass link,
the access list checked on every call, CSRF and owner-only routes, page
security headers, validation, and a streamed run with a scripted model."""

import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

pytest.importorskip("httpx")
from starlette.testclient import TestClient  # noqa: E402

import playground.app as pg  # noqa: E402
from test_playground_core import BAD_ATTR, VALID, ScriptedLLM  # noqa: E402

H = {"X-Playground": "1", "Content-Type": "application/json"}
OWNER = "ownerpass"


class _Scripted(ScriptedLLM):
    async def close(self):
        pass


@pytest.fixture
def client(monkeypatch):
    monkeypatch.setattr(pg.settings, "store", "memory")
    monkeypatch.setattr(pg.settings, "session_key", "test-session-key")
    monkeypatch.setattr(pg.settings, "bootstrap_admin_pass", OWNER)
    monkeypatch.setattr(pg.settings, "bq_table", "")
    monkeypatch.setattr(pg.settings, "admin_emails", ())
    monkeypatch.setattr(pg.settings, "oauth_client_id", "")
    pg._forget()
    with TestClient(pg.app, base_url="https://testserver") as c:
        c.scripted = None

        def script(answers, **kw):
            pg.S.llm = _Scripted(answers, **kw)
            return pg.S.llm
        c.script = script
        yield c
    pg._forget()


def _owner(c):
    r = c.get("/p/" + pg.S.signer.pass_token(OWNER), follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"
    return c


def _events(resp):
    out = []
    for block in resp.text.split("\n\n"):
        for line in block.split("\n"):
            if line.startswith("data: "):
                out.append(json.loads(line[6:]))
    return out


def test_signed_out_gets_the_sign_in_page_and_401s(client):
    r = client.get("/")
    assert r.status_code == 200 and "signin" in r.text.lower()
    assert client.get("/api/me").status_code == 401
    assert client.post("/api/validate", headers=H, json={"x3d": VALID}).status_code == 401
    assert client.get("/admin", follow_redirects=False).status_code == 303


def test_pages_carry_strict_security_headers(client):
    r = client.get("/")
    csp = r.headers["content-security-policy"]
    assert "frame-ancestors 'none'" in csp and "object-src 'none'" in csp
    v = client.get("/viewer")
    vcsp = v.headers["content-security-policy"]
    assert "default-src 'none'" in vcsp and "form-action 'none'" in vcsp
    assert r.headers["x-content-type-options"] == "nosniff"


def test_forged_or_unknown_pass_is_refused_and_logged(client):
    r = client.get("/p/" + OWNER + ".forged", follow_redirects=False)
    assert r.status_code == 303 and "denied=pass" in r.headers["location"]
    assert pg.auth.COOKIE not in client.cookies
    events = pg.S.store.events
    assert any(e["kind"] == "pass-denied" for e in events)


def test_owner_pass_signs_in_with_a_hardened_cookie(client):
    r = client.get("/p/" + pg.S.signer.pass_token(OWNER), follow_redirects=False)
    cookie = r.headers["set-cookie"]
    assert "HttpOnly" in cookie and "Secure" in cookie and "samesite=lax" in cookie.lower()
    me = client.get("/api/me").json()
    assert me["admin"] is True and me["me"]["kind"] == "pass"
    assert me["validator"] and me["xite"]
    assert all("model" not in w for w in me["writers"])     # model ids stay server-side


def test_writes_need_the_csrf_header(client):
    _owner(client)
    r = client.post("/api/validate", json={"x3d": VALID})
    assert r.status_code == 400
    r = client.post("/api/validate", headers=H, json={"x3d": VALID})
    assert r.status_code == 200 and r.json()["report"]["verdict"] == "pass"


def test_validate_reports_the_exact_defect(client):
    _owner(client)
    rep = client.post("/api/validate", headers=H, json={"x3d": BAD_ATTR}).json()["report"]
    assert rep["verdict"] == "fail"
    assert any("diffuseColour" in f["message"] and f["line"] == 6 for f in rep["findings"])


def test_guest_pass_is_not_an_admin_and_revocation_is_immediate(client):
    _owner(client)
    made = client.post("/api/admin/passes", headers=H, json={"label": "Guest", "days": 1}).json()
    assert made["link"].startswith("/p/")
    guest = TestClient(pg.app, base_url="https://testserver")
    guest.get(made["link"], follow_redirects=False)
    assert guest.get("/api/me").json()["admin"] is False
    assert guest.get("/api/admin/overview").status_code == 403
    assert client.post(f"/api/admin/passes/{made['id']}/revoke", headers=H).status_code == 200
    r = guest.get("/api/me")
    assert r.status_code == 401
    assert guest.get("/").text.lower().count("signin") >= 1


def test_access_list_edits_apply_to_existing_google_sessions(client):
    _owner(client)
    client.put("/api/admin/access", headers=H, json={"domains": ["vt.edu"], "emails": []})
    data = {"id": pg.auth.email_id("prof@cs.vt.edu"), "kind": "google", "display": "prof@cs.vt.edu",
            "email": "prof@cs.vt.edu", "domain": "cs.vt.edu", "admin": False}
    person = TestClient(pg.app, base_url="https://testserver",
                        cookies={pg.auth.COOKIE: pg.S.signer.cookie(data)})
    assert person.get("/api/me").status_code == 200
    client.put("/api/admin/access", headers=H, json={"blocked": ["prof@cs.vt.edu"]})
    assert person.get("/api/me").status_code == 401
    client.put("/api/admin/access", headers=H, json={"blocked": [], "domains": []})
    assert person.get("/api/me").status_code == 401


def test_google_sign_in_is_off_until_a_client_id_is_set(client):
    r = client.post("/auth/google", headers=H, json={"credential": "x"})
    assert r.status_code == 400 and "pass link" in r.json()["error"]
    assert client.get("/auth/config").json()["oauth_client_id"] == ""


def test_streamed_run_repairs_from_the_report_and_is_stored(client):
    _owner(client)
    llm = client.script([BAD_ATTR, VALID])
    r = client.post("/api/run", headers=H, json={"prompt": "a red box", "writer": "a", "autofix": False})
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
    evs = _events(r)
    final = next(e for e in evs if e["t"] == "final")
    assert final["verdict"] == "pass"
    assert any(e["t"] == "judge" for e in evs)
    assert not any(e["t"] in ("_record", "_ping") for e in evs)
    run_id = evs[0]["run_id"]
    stored = client.get(f"/api/runs/{run_id}").json()
    assert stored["final_verdict"] == "pass" and stored["first_verdict"] == "fail"
    assert stored["judge"]["compare"]["validator_verdict"] == "invalid"
    assert "principal" not in stored
    assert len(llm.writer_turns) == 2
    # the browser's verdict lands on the run
    v = client.post(f"/api/runs/{run_id}/visible", headers=H,
                    json={"status": "ok", "coverage": 0.2, "thumb": "data:image/jpeg;base64,AAAA"})
    assert v.status_code == 200
    assert client.get(f"/api/runs/{run_id}").json()["visible"]["status"] == "ok"
    hist = client.get("/api/history").json()["runs"]
    assert hist and hist[0]["run_id"] == run_id
    # the one-run lease was released
    client.script([VALID])
    assert client.post("/api/run", headers=H, json={"prompt": "again", "writer": "a"}).status_code == 200


def test_other_people_cannot_read_your_runs(client):
    _owner(client)
    client.script([VALID])
    evs = _events(client.post("/api/run", headers=H, json={"prompt": "x", "writer": "a", "judge": "off"}))
    run_id = evs[0]["run_id"]
    made = client.post("/api/admin/passes", headers=H, json={"label": "Guest", "days": 1}).json()
    guest = TestClient(pg.app, base_url="https://testserver")
    guest.get(made["link"], follow_redirects=False)
    assert guest.get(f"/api/runs/{run_id}").status_code == 404
    assert guest.post(f"/api/runs/{run_id}/visible", headers=H, json={"status": "ok"}).status_code == 404


def test_run_limits_and_pause(client):
    _owner(client)
    client.put("/api/admin/access", headers=H, json={"paused": True})
    r = client.post("/api/run", headers=H, json={"prompt": "x", "writer": "a"})
    assert r.status_code == 429 and r.json()["reason"] == "paused"
    client.put("/api/admin/access", headers=H, json={"paused": False, "limits": {"user_per_hour": 1}})
    client.script([VALID])
    assert client.post("/api/run", headers=H, json={"prompt": "x", "writer": "a", "judge": "off"}).status_code == 200
    r = client.post("/api/run", headers=H, json={"prompt": "x", "writer": "a"})
    assert r.status_code == 429 and r.json()["reason"] == "user_hour" and "retry-after" in r.headers
    assert any(e["kind"] == "rate-limited" for e in pg.S.store.events)


def test_run_input_checks(client):
    _owner(client)
    assert client.post("/api/run", headers=H, json={"writer": "a"}).status_code == 400
    assert client.post("/api/run", headers=H, json={"prompt": "x" * 5000, "writer": "a"}).status_code == 413
    assert client.post("/api/run", headers=H, json={"prompt": "x", "writer": "f"}).status_code == 400
    client.put("/api/admin/access", headers=H, json={"writers_off": ["a"]})
    assert client.post("/api/run", headers=H, json={"prompt": "x", "writer": "a"}).status_code == 400


def test_mutate_endpoint(client):
    _owner(client)
    out = client.post("/api/mutate", headers=H, json={"x3d": VALID, "op": "drop-def"}).json()
    assert "applied" in out
    assert client.post("/api/mutate", headers=H, json={"x3d": VALID, "op": "nope"}).status_code == 400


def test_admin_overview_shows_runs_events_and_passes(client):
    _owner(client)
    client.script([VALID])
    client.post("/api/run", headers=H, json={"prompt": "x", "writer": "a", "judge": "off"})
    o = client.get("/api/admin/overview").json()
    assert o["runs"] and "x3d" not in o["runs"][0]
    assert any(p["id"] == OWNER for p in o["passes"])
    assert any(e["kind"] == "signin" for e in o["events"])
    assert o["limits"]["usd_phase"] == 10.0


def test_client_ip_uses_the_entry_the_front_end_appended(client):
    class R:
        headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.7"}
        client = None
    assert pg._ip(R()) == "203.0.113.7"
