"""Playground settings. Everything that differs between a laptop and Cloud Run
comes from the environment; nothing here names a project, account or URL.

Writers and judges are addressed by a short public code. The page, the API
and the run log's public fields only ever carry the code and its label; the
model id stays on the server.
"""

from __future__ import annotations

import os
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parent
STATIC = ROOT / "static"


def _env(name: str, default: str = "") -> str:
    return os.environ.get(name, default).strip()


def _env_int(name: str, default: int) -> int:
    try:
        return int(_env(name, str(default)))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(_env(name, str(default)))
    except ValueError:
        return default


@dataclass(frozen=True)
class Writer:
    code: str          # public id, e.g. "a"
    label: str         # public name on the page
    blurb: str         # public one-liner
    kind: str          # "gemini" | "openai-compat" | "openai"
    model: str         # server-side only
    family: str        # judges come from a different family than the writer
    usd_in: float      # USD per million input tokens (conservative estimate)
    usd_out: float     # USD per million output tokens, thinking included
    max_out: int = 12000
    enabled: bool = True
    notes: str = ""


# Prices are deliberately rounded up from the published Vertex AI rates so the
# spend guard trips early, never late. Check the pricing page before relying on
# them for anything but a ceiling.
WRITERS: dict[str, Writer] = {w.code: w for w in [
    Writer("a", "Writer A", "fast frontier model", "gemini", "gemini-3.8-flash",
           "g", 0.75, 4.0),
    Writer("b", "Writer B", "deep frontier model", "gemini", "gemini-3.1-pro-preview",
           "g", 2.5, 15.0, max_out=16000),
    Writer("c", "Writer C", "open-weight coder", "openai-compat",
           "qwen/qwen3-coder-480b-a35b-instruct-maas", "q", 1.0, 4.0),
    Writer("d", "Writer D", "open-weight generalist", "openai-compat",
           "deepseek-ai/deepseek-v3.2-maas", "d", 1.0, 2.0),
    Writer("e", "Writer E", "open-weight reasoner", "openai-compat",
           "openai/gpt-oss-120b-maas", "o", 0.3, 1.2, max_out=16000),
    Writer("f", "Writer F", "frontier model, second family", "openai",
           _env("PLAYGROUND_OPENAI_MODEL", "gpt-6.1-sol"), "o", 2.5, 15.0,
           enabled=bool(_env("OPENAI_API_KEY")),
           notes="needs OPENAI_API_KEY from Secret Manager"),
]}

DEFAULT_WRITER = "a"


def judge_for(writer_code: str, requested: str = "") -> Writer:
    """The judge for a run: the requested one if it is from another family,
    else the first enabled writer from another family."""
    w = WRITERS[writer_code]
    if requested in WRITERS and WRITERS[requested].enabled and WRITERS[requested].family != w.family:
        return WRITERS[requested]
    order = ["a", "d", "c", "e", "b", "f"] if w.family != "g" else ["d", "c", "e", "f"]
    for code in order:
        cand = WRITERS[code]
        if cand.enabled and cand.family != w.family:
            return cand
    return WRITERS[DEFAULT_WRITER]


def git_commit() -> str:
    """The validator commit behind every score: baked in at build time."""
    explicit = _env("PLAYGROUND_VALIDATOR_COMMIT")
    if explicit:
        return explicit
    try:
        return subprocess.run(["git", "-C", str(ROOT.parent), "rev-parse", "--short", "HEAD"],
                              capture_output=True, text=True, timeout=5).stdout.strip() or "unknown"
    except Exception:
        return "unknown"


@dataclass
class Limits:
    """Defaults; the admin page overrides any of them at run time."""
    user_per_hour: int = 12
    user_per_day: int = 40
    ip_per_hour: int = 30
    global_runs_per_day: int = 400
    usd_per_day: float = 3.0
    usd_phase: float = 10.0
    validate_per_hour: int = 120        # paste-and-validate, no model call
    max_prompt_chars: int = 1200
    max_scene_chars: int = 200_000
    max_repairs: int = 3
    run_timeout_s: int = 240

    def as_dict(self) -> dict:
        return dict(self.__dict__)


@dataclass
class Settings:
    project: str = field(default_factory=lambda: _env("PLAYGROUND_PROJECT") or _env("GOOGLE_CLOUD_PROJECT"))
    vertex_location: str = field(default_factory=lambda: _env("PLAYGROUND_VERTEX_LOCATION", "global"))
    store: str = field(default_factory=lambda: _env("PLAYGROUND_STORE", "memory"))   # memory | journal | firestore
    firestore_db: str = field(default_factory=lambda: _env("PLAYGROUND_FIRESTORE_DB", "playground"))
    bq_table: str = field(default_factory=lambda: _env("PLAYGROUND_BQ_TABLE"))        # dataset.table
    session_key: str = field(default_factory=lambda: _env("PLAYGROUND_SESSION_KEY"))
    admin_emails: tuple = field(default_factory=lambda: tuple(
        e.strip().lower() for e in _env("PLAYGROUND_ADMIN_EMAILS").split(",") if e.strip()))
    oauth_client_id: str = field(default_factory=lambda: _env("PLAYGROUND_OAUTH_CLIENT_ID"))
    bootstrap_admin_pass: str = field(default_factory=lambda: _env("PLAYGROUND_BOOTSTRAP_PASS"))
    static_token: str = field(default_factory=lambda: _env("PLAYGROUND_STATIC_TOKEN"))  # local dev only
    xite_version: str = field(default_factory=lambda: _env("PLAYGROUND_XITE_VERSION", "16.4.3"))
    xite_dir: str = field(default_factory=lambda: _env("PLAYGROUND_XITE_DIR", str(ROOT / "vendor" / "x_ite")))
    secure_cookies: bool = field(default_factory=lambda: _env("PLAYGROUND_INSECURE_COOKIES") != "1")
    phase_label: str = field(default_factory=lambda: _env("PLAYGROUND_PHASE", "2026-10"))
    validator_commit: str = field(default_factory=git_commit)
    limits: Limits = field(default_factory=lambda: Limits(
        user_per_hour=_env_int("PLAYGROUND_USER_PER_HOUR", 12),
        user_per_day=_env_int("PLAYGROUND_USER_PER_DAY", 40),
        ip_per_hour=_env_int("PLAYGROUND_IP_PER_HOUR", 30),
        global_runs_per_day=_env_int("PLAYGROUND_GLOBAL_PER_DAY", 400),
        usd_per_day=_env_float("PLAYGROUND_USD_PER_DAY", 3.0),
        usd_phase=_env_float("PLAYGROUND_USD_PHASE", 10.0),
    ))


def public_writers() -> list[dict]:
    return [{"code": w.code, "label": w.label, "blurb": w.blurb, "enabled": w.enabled}
            for w in WRITERS.values()]


def cost_usd(w: Writer, tokens_in: int, tokens_out: int) -> float:
    return (tokens_in * w.usd_in + tokens_out * w.usd_out) / 1_000_000
