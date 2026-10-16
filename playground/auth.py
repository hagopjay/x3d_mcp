"""Who may use the playground.

Two ways in, one session cookie out:

* **Sign in with Google.** The page gets a Google ID token; the server checks
  its signature, audience and ``email_verified``, then the address against
  the access list: ``blocked`` first, then exact ``emails``, then ``domains``
  (``vt.edu`` admits ``x@vt.edu`` and ``x@cs.vt.edu``; a Google Workspace
  ``hd`` claim counts too).
* **Pass links.** ``/p/<id>.<sig>`` where the signature is an HMAC of the id
  under the session key, so a pass cannot be guessed, and the pass record
  (label, expiry, limits, revoked) lives in the store, so it can be revoked.

The cookie is ``<base64 json>.<hmac>``: HttpOnly, Secure, SameSite=Lax,
12 hours. Every state-changing call also needs the ``X-Playground`` header,
which a cross-site form or a sandboxed scene cannot send without a CORS
preflight the server never grants.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from dataclasses import dataclass

COOKIE = "pg_session"
SESSION_SECONDS = 12 * 3600
CSRF_HEADER = "x-playground"


@dataclass
class Principal:
    id: str              # stable: "g:<sha256(email)[:16]>" or "p:<pass id>"
    kind: str            # "google" | "pass"
    display: str         # the address or the pass label; shown to the admin only
    email: str = ""
    domain: str = ""
    admin: bool = False
    pass_id: str = ""
    per_hour: int | None = None
    per_day: int | None = None
    hd: str = ""         # Google Workspace domain claim, when there is one

    def public(self) -> dict:
        return {"kind": self.kind, "display": self.display, "admin": self.admin}


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    def __init__(self, key: str):
        self.key = key.encode() if key else secrets.token_bytes(32)

    def sign(self, payload: str) -> str:
        return _b64(hmac.new(self.key, payload.encode(), hashlib.sha256).digest()[:24])

    def verify(self, payload: str, sig: str) -> bool:
        return hmac.compare_digest(self.sign(payload), sig)

    def cookie(self, data: dict) -> str:
        body = _b64(json.dumps({**data, "exp": int(time.time()) + SESSION_SECONDS},
                               separators=(",", ":")).encode())
        return f"{body}.{self.sign(body)}"

    def read_cookie(self, value: str) -> dict | None:
        if not value or "." not in value:
            return None
        body, sig = value.rsplit(".", 1)
        if not self.verify(body, sig):
            return None
        try:
            data = json.loads(_unb64(body))
        except ValueError:
            return None
        if data.get("exp", 0) < time.time():
            return None
        return data

    def pass_token(self, pid: str) -> str:
        return f"{pid}.{self.sign('pass:' + pid)}"

    def read_pass_token(self, token: str) -> str | None:
        if "." not in token:
            return None
        pid, sig = token.split(".", 1)
        return pid if self.verify("pass:" + pid, sig) else None


def email_id(email: str) -> str:
    return "g:" + hashlib.sha256(email.lower().encode()).hexdigest()[:16]


def _domain_match(domain: str, rule: str) -> bool:
    rule = rule.lower().lstrip("@").lstrip("*.").strip()
    return bool(rule) and (domain == rule or domain.endswith("." + rule))


def check_access(email: str, hd: str, access: dict) -> tuple[bool, str]:
    """(allowed, reason) for a verified address against the access list."""
    email = email.lower().strip()
    domain = email.rsplit("@", 1)[-1] if "@" in email else ""
    hd = (hd or "").lower()
    for rule in access.get("blocked", []):
        rule = rule.lower().strip()
        if rule == email or ("@" not in rule and _domain_match(domain, rule)):
            return False, "blocked"
    if email in {e.lower().strip() for e in access.get("emails", [])}:
        return True, "address"
    if email in {e.lower().strip() for e in access.get("admins", [])}:
        return True, "admin"
    for rule in access.get("domains", []):
        if _domain_match(domain, rule) or (hd and _domain_match(hd, rule)):
            return True, f"domain {rule.lower().lstrip('@')}"
    return False, "not on the list"


def verify_google_token(credential: str, client_id: str) -> dict:
    """Check a Google ID token; returns its claims. Raises ValueError."""
    from google.auth.transport import requests as gar
    from google.oauth2 import id_token
    claims = id_token.verify_oauth2_token(credential, gar.Request(), audience=client_id)
    if claims.get("iss") not in ("accounts.google.com", "https://accounts.google.com"):
        raise ValueError("wrong issuer")
    if not claims.get("email") or not claims.get("email_verified"):
        raise ValueError("the Google account has no verified address")
    return claims


def principal_from_session(data: dict | None) -> Principal | None:
    if not data:
        return None
    return Principal(id=data["id"], kind=data["kind"], display=data.get("display", ""),
                     email=data.get("email", ""), domain=data.get("domain", ""),
                     admin=bool(data.get("admin")), pass_id=data.get("pass_id", ""),
                     per_hour=data.get("per_hour"), per_day=data.get("per_day"),
                     hd=data.get("hd", ""))


def new_pass_id() -> str:
    return secrets.token_urlsafe(9).replace("-", "x").replace("_", "y")


def ip_key(ip: str, signer: Signer) -> str:
    """IPs are kept as a keyed hash: enough to rate-limit and to spot abuse,
    not a stored address."""
    return signer.sign("ip:" + (ip or "-"))[:16]
