"""The judge arena, live: a model from another family reviews the first draft
without the validator, and its answer is set beside the validator's.

Matching a judge's prose defects to the validator's findings is heuristic and
labelled as such on the page: a judge defect matches a validator error when
it points at the same line (plus or minus one) or names a token the validator
quotes ('colour', 'set_rotaton', a DEF name).
"""

from __future__ import annotations

import json
import re

from playground import prompts
from playground.config import Writer, cost_usd

_QUOTED = re.compile(r"'([^']{2,60})'")
_SKIP_TOKENS = {"error", "warning", "element", "attribute", "true", "false", "x3d", "scene", "head"}


def _parse(text: str) -> dict | None:
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("\n", 1)[-1].rsplit("```", 1)[0]
    try:
        return json.loads(t)
    except ValueError:
        a, b = t.find("{"), t.rfind("}")
        if a >= 0 and b > a:
            try:
                return json.loads(t[a:b + 1])
            except ValueError:
                return None
    return None


def _tokens(message: str) -> set[str]:
    return {m.lower() for m in _QUOTED.findall(message) if m.lower() not in _SKIP_TOKENS}


def compare(judge_out: dict, report: dict) -> dict:
    errors = [f for f in report["findings"] if f["level"] == "error"]
    defects = judge_out.get("defects") or []
    if not isinstance(defects, list):
        defects = []
    matched_judge: set[int] = set()
    caught = []
    for i, v in enumerate(errors):
        toks = _tokens(v["message"])
        hit = None
        for j, d in enumerate(defects):
            if not isinstance(d, dict):
                continue
            line = d.get("line")
            words = f"{d.get('what', '')} {d.get('fix', '')}".lower()
            same_line = isinstance(line, (int, float)) and v.get("line") and abs(int(line) - v["line"]) <= 1
            same_token = any(t in words for t in toks)
            if same_line or same_token:
                hit = j
                break
        if hit is not None:
            matched_judge.add(hit)
            caught.append(i)
    verdict = str(judge_out.get("verdict", "")).lower()
    validator_valid = report["verdict"] == "pass"
    judge_valid = verdict == "valid"
    return {
        "judge_verdict": verdict or "unparsed",
        "validator_verdict": "valid" if validator_valid else "invalid",
        "agree": (judge_valid == validator_valid) if verdict in ("valid", "invalid") else None,
        "validator_errors": len(errors),
        "judge_defects": len(defects),
        "caught": len(caught),
        "missed": len(errors) - len(caught),
        "unmatched_claims": len(defects) - len(matched_judge),
        "caught_index": caught,
        "method": "line within 1, or a token the validator quotes",
    }


async def run_judge(llm, judge: Writer, text: str, report: dict) -> dict:
    out, usage = await llm.complete(
        judge, prompts.SYSTEM_JUDGE,
        [{"role": "user", "text": prompts.JUDGE_USER.format(numbered=prompts.numbered(text))}],
        max_out=min(judge.max_out, 8000), temperature=0.0, json_mode=judge.kind != "openai-compat")
    parsed = _parse(out)
    usd = cost_usd(judge, usage.tokens_in, usage.tokens_out)
    if parsed is None:
        return {"error": "the judge's answer was not JSON", "raw": out[:600], "usd": usd,
                "tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out, "ms": usage.ms}
    defects = parsed.get("defects") if isinstance(parsed.get("defects"), list) else []
    return {"verdict": parsed.get("verdict"), "defects": defects[:30],
            "compare": compare(parsed, report), "usd": usd,
            "tokens_in": usage.tokens_in, "tokens_out": usage.tokens_out, "ms": usage.ms}
