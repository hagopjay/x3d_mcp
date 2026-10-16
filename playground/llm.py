"""Streaming calls to the writers and judges.

Three wire formats cover every writer in config.WRITERS:

* ``gemini``         Vertex AI streamGenerateContent (server-sent events)
* ``openai-compat``  Vertex AI Model Garden's OpenAI-compatible endpoint
* ``openai``         the OpenAI chat completions API (needs OPENAI_API_KEY)

Vertex calls carry the service identity's access token, so no model key ever
leaves Secret Manager. Every call streams: long answers survive proxies that
cut a response after 30 s, and the page can show the scene as it is written.
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from dataclasses import dataclass
from typing import AsyncIterator

import httpx

from playground.config import Settings, Writer


@dataclass
class Usage:
    tokens_in: int = 0
    tokens_out: int = 0
    ms: int = 0
    finish: str = ""
    retries: int = 0


RETRY_DELAYS = (2.0, 6.0, 14.0)
_BUSY = {429, 500, 502, 503, 504}


class Busy(Exception):
    """The endpoint is overloaded; worth one more try."""


async def _check(r) -> None:
    if r.status_code == 200:
        return
    body = (await r.aread())[:300]
    msg = f"writer returned HTTP {r.status_code}: {body!r}"
    if r.status_code in _BUSY:
        raise Busy(msg)
    raise RuntimeError(msg)


class _TokenSource:
    """An access token for Vertex AI: a fixed token in local development,
    else Application Default Credentials (the Cloud Run service identity)."""

    def __init__(self, settings: Settings):
        self._static = settings.static_token
        self._creds = None
        self._lock = asyncio.Lock()
        self.project = settings.project

    async def token(self) -> str:
        if self._static:
            return self._static
        async with self._lock:
            if self._creds is None:
                import google.auth
                creds, project = await asyncio.to_thread(
                    google.auth.default, scopes=["https://www.googleapis.com/auth/cloud-platform"])
                self._creds = creds
                self.project = self.project or project or ""
            if not self._creds.valid:
                import google.auth.transport.requests
                await asyncio.to_thread(self._creds.refresh, google.auth.transport.requests.Request())
            return self._creds.token


def _ca_verify():
    bundle = os.environ.get("SSL_CERT_FILE") or os.environ.get("REQUESTS_CA_BUNDLE")
    return bundle if bundle and os.path.exists(bundle) else True


class LLM:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.tokens = _TokenSource(settings)
        self.http = httpx.AsyncClient(timeout=httpx.Timeout(240.0, connect=20.0),
                                      verify=_ca_verify())

    async def close(self):
        await self.http.aclose()

    def _vertex_host(self) -> str:
        loc = self.settings.vertex_location
        return "aiplatform.googleapis.com" if loc == "global" else f"{loc}-aiplatform.googleapis.com"

    async def stream(self, w: Writer, system: str, turns: list[dict], *,
                     max_out: int | None = None, temperature: float = 0.4,
                     json_mode: bool = False, usage: Usage | None = None) -> AsyncIterator[str]:
        """Yield text deltas. ``turns`` alternates {"role": "user"|"model", "text"}.
        ``usage`` is filled in when the stream ends."""
        usage = usage if usage is not None else Usage()
        t0 = time.perf_counter()
        max_out = max_out or w.max_out
        if w.kind not in ("gemini", "openai-compat", "openai"):
            raise ValueError(f"unknown writer kind {w.kind}")
        try:
            for attempt in range(len(RETRY_DELAYS) + 1):
                gen = (self._gemini if w.kind == "gemini" else self._openai)(
                    w, system, turns, max_out, temperature, json_mode, usage)
                started = False
                try:
                    async for piece in gen:
                        started = True
                        yield piece
                    return
                except (Busy, httpx.TransportError) as exc:
                    # Shared model endpoints answer 429/5xx under load. Retry only
                    # before the first token, so the page never sees a repeat.
                    if started or attempt == len(RETRY_DELAYS):
                        raise RuntimeError(str(exc)) from None
                    usage.retries += 1
                    await asyncio.sleep(RETRY_DELAYS[attempt])
        finally:
            usage.ms = int((time.perf_counter() - t0) * 1000)

    async def complete(self, w: Writer, system: str, turns: list[dict], **kw) -> tuple[str, Usage]:
        usage = Usage()
        parts = [p async for p in self.stream(w, system, turns, usage=usage, **kw)]
        return "".join(parts), usage

    async def _gemini(self, w, system, turns, max_out, temperature, json_mode, usage):
        token = await self.tokens.token()
        project = self.tokens.project
        url = (f"https://{self._vertex_host()}/v1/projects/{project}/locations/"
               f"{self.settings.vertex_location}/publishers/google/models/{w.model}"
               ":streamGenerateContent?alt=sse")
        gc = {"maxOutputTokens": max_out, "temperature": temperature,
              "thinkingConfig": {"thinkingLevel": "LOW"}}
        if json_mode:
            gc["responseMimeType"] = "application/json"
        body = {"contents": [{"role": "model" if t["role"] == "model" else "user",
                              "parts": [{"text": t["text"]}]} for t in turns],
                "generationConfig": gc,
                "systemInstruction": {"parts": [{"text": system}]}}
        async with self.http.stream("POST", url, json=body,
                                    headers={"Authorization": f"Bearer {token}"}) as r:
            await _check(r)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                try:
                    d = json.loads(line[5:].strip())
                except ValueError:
                    continue
                u = d.get("usageMetadata")
                if u:
                    usage.tokens_in = u.get("promptTokenCount", 0)
                    usage.tokens_out = u.get("candidatesTokenCount", 0) + u.get("thoughtsTokenCount", 0)
                for c in d.get("candidates", []) or []:
                    if c.get("finishReason"):
                        usage.finish = c["finishReason"]
                    for p in (c.get("content") or {}).get("parts", []) or []:
                        if p.get("text") and not p.get("thought"):
                            yield p["text"]

    async def _openai(self, w, system, turns, max_out, temperature, json_mode, usage):
        msgs = [{"role": "system", "content": system}] + [
            {"role": "assistant" if t["role"] == "model" else "user", "content": t["text"]}
            for t in turns]
        body = {"model": w.model, "messages": msgs, "stream": True,
                "stream_options": {"include_usage": True}}
        if w.kind == "openai":
            url = "https://api.openai.com/v1/chat/completions"
            headers = {"Authorization": f"Bearer {os.environ.get('OPENAI_API_KEY', '')}"}
            body["max_completion_tokens"] = max_out
        else:
            token = await self.tokens.token()
            url = (f"https://{self._vertex_host()}/v1/projects/{self.tokens.project}/locations/"
                   f"{self.settings.vertex_location}/endpoints/openapi/chat/completions")
            headers = {"Authorization": f"Bearer {token}"}
            body["max_tokens"] = max_out
            body["temperature"] = temperature
        if json_mode:
            body["response_format"] = {"type": "json_object"}
        async with self.http.stream("POST", url, json=body, headers=headers) as r:
            await _check(r)
            async for line in r.aiter_lines():
                if not line.startswith("data:"):
                    continue
                data = line[5:].strip()
                if data == "[DONE]":
                    break
                try:
                    d = json.loads(data)
                except ValueError:
                    continue
                u = d.get("usage")
                if u:
                    usage.tokens_in = u.get("prompt_tokens", 0) or 0
                    usage.tokens_out = u.get("completion_tokens", 0) or 0
                for c in d.get("choices", []) or []:
                    if c.get("finish_reason"):
                        usage.finish = c["finish_reason"]
                    piece = (c.get("delta") or {}).get("content")
                    if piece:
                        yield piece
