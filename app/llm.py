"""Model calls for the AI features, following the fleet llm.py contract (~/.claude/skills/fleet-llm/SKILL.md).

Provider precedence, checked on every call (not at import): OPENROUTER_API_KEY, then ANTHROPIC_API_KEY, else
unavailable. Every gate failure raises LLMUnavailable with a plain-English reason the pages can show:

  - LLM_ENABLED is 0/false/no/off               (kill switch, no API call, nothing recorded)
  - Firm.ai_enabled is off                       (the owner has not turned AI on in Settings)
  - no API key in the environment
  - LLM_DAILY_CAP model calls already made today (UTC; only real network calls count)
  - AI_DAILY_CAP_CENTS estimated spend reached today

Every network call writes one AiRun row (kind, entity, chars, model, estimated cost, ok flag). Failures are logged
with provider, HTTP status and the first 200 characters of the body, recorded with ok=False, and re-raised as
LLMUnavailable so callers take their fallback. Callers must catch LLMUnavailable.

Context is capped at MAX_CONTEXT_CHARS (12,000); longer prompts are cut and a note is appended so the model knows.

Model pins live in Config.AI_MODEL (direct id) and Config.AI_OPENROUTER_MODEL (OpenRouter id), side by side, so a
model change is one edit. No date suffixes. Sampling parameters are never sent unless a caller asks, and then only
to models that accept them (accepts_sampling). JSON calls use structured outputs on the direct path and a prompt
line plus _extract_json on OpenRouter. Thinking models (Opus 5, Sonnet 5, Fable, Mythos) get effort=low and at
least 4096 max_tokens. stop_reason == "refusal" is treated as a failed call.
"""
import json
import logging
import math
import os
import re
from datetime import datetime, timezone

import requests
from flask import current_app, has_app_context

from .extensions import db
from .models import AiRun, Firm

log = logging.getLogger("llm")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
ANTHROPIC_URL = "https://api.anthropic.com/v1/messages"
ANTHROPIC_VERSION = "2023-06-01"
TIMEOUT = (10, 90)

MAX_CONTEXT_CHARS = 12000
TRUNCATION_NOTE = "\n\n[Context was cut at 12,000 characters. Work from what is above.]"

# $/M tokens from the fleet pin table, in cents per million (in, out). Unknown models use the Sonnet 4.6 line.
PRICES_CENTS_PER_M = {
    "claude-haiku-4-5": (100, 500),
    "claude-sonnet-4-6": (300, 1500),
    "claude-sonnet-5": (200, 1000),
    "claude-opus-4-8": (500, 2500),
    "claude-opus-5": (500, 2500),
}
_DEFAULT_PRICE = (300, 1500)

_NO_SAMPLING_PREFIXES = ("claude-opus-4-7", "claude-opus-4-8", "claude-opus-5",
                         "claude-sonnet-5", "claude-fable", "claude-mythos")
_THINKING_PREFIXES = ("claude-opus-5", "claude-sonnet-5", "claude-fable", "claude-mythos")
_FALSEY = ("0", "false", "no", "off")


class LLMUnavailable(Exception):
    """The model cannot be used right now. str(exc) is safe to show to staff."""


class LLMBadOutput(LLMUnavailable):
    """The model answered but not in the shape the feature asked for."""


# ---------------------------------------------------------------------------
# settings, read live
# ---------------------------------------------------------------------------
def _setting(name, default=""):
    """Environment first, then app config (so tests can inject), then the default."""
    v = os.environ.get(name)
    if v not in (None, ""):
        return v
    if has_app_context():
        cv = current_app.config.get(name)
        if cv not in (None, ""):
            return cv
    return default


def _bare(model):
    """anthropic/claude-opus-4.8 -> claude-opus-4-8 ; claude-sonnet-5 -> claude-sonnet-5."""
    return (model or "").split("/", 1)[-1].replace(".", "-")


def accepts_sampling(model):
    """False for models that answer 400 to temperature/top_p/top_k. Handles both id styles."""
    return not _bare(model).startswith(_NO_SAMPLING_PREFIXES)


def is_thinking_model(model):
    return _bare(model).startswith(_THINKING_PREFIXES)


def enabled():
    return str(_setting("LLM_ENABLED", "true")).strip().lower() not in _FALSEY


def provider():
    """"openrouter", "anthropic" or None."""
    if _setting("OPENROUTER_API_KEY"):
        return "openrouter"
    if _setting("ANTHROPIC_API_KEY"):
        return "anthropic"
    return None


def model_for(prov):
    if prov == "openrouter":
        return _setting("AI_OPENROUTER_MODEL", "anthropic/claude-haiku-4.5")
    return _setting("AI_MODEL", "claude-haiku-4-5")


def daily_cap_cents():
    try:
        return int(_setting("AI_DAILY_CAP_CENTS", 300) or 0)
    except (TypeError, ValueError):
        return 300


def daily_cap_calls():
    try:
        return int(_setting("LLM_DAILY_CAP", 0) or 0)
    except (TypeError, ValueError):
        return 0


def _utc_day_start():
    n = datetime.now(timezone.utc)
    return datetime(n.year, n.month, n.day)  # naive UTC, matching AiRun.created_at


def spent_today_cents():
    v = db.session.query(db.func.coalesce(db.func.sum(AiRun.cost_cents), 0)).filter(
        AiRun.created_at >= _utc_day_start()).scalar()
    return int(v or 0)


def calls_today():
    return AiRun.query.filter(AiRun.created_at >= _utc_day_start()).count()


def status():
    """For the AI index page and settings. Never raises."""
    prov = provider()
    firm_on = bool(Firm.get().ai_enabled)
    reason = ""
    if not enabled():
        reason = "LLM_ENABLED is off in the environment."
    elif not firm_on:
        reason = "AI features are turned off in Settings."
    elif not prov:
        reason = "No API key is set (OPENROUTER_API_KEY or ANTHROPIC_API_KEY)."
    return dict(enabled=enabled(), firm_on=firm_on, provider=prov, model=model_for(prov) if prov else "",
                spent_today_cents=spent_today_cents(), cap_cents=daily_cap_cents(), calls_today=calls_today(),
                cap_calls=daily_cap_calls(), available=not reason, reason=reason)


def clip(text, limit=MAX_CONTEXT_CHARS):
    """Return (text, truncated). Used by features that want to tell staff the input was cut."""
    text = text or ""
    if len(text) <= limit:
        return text, False
    return text[:limit], True


def estimate_cost_cents(model, in_tokens, out_tokens):
    """Whole cents, rounded up, so the daily cap errs on the safe side."""
    p_in, p_out = _DEFAULT_PRICE
    bare = _bare(model)
    for k, v in PRICES_CENTS_PER_M.items():
        if bare.startswith(k):
            p_in, p_out = v
            break
    cents = (in_tokens * p_in + out_tokens * p_out) / 1_000_000
    return max(1, int(math.ceil(cents)))


# ---------------------------------------------------------------------------
# the call
# ---------------------------------------------------------------------------
def _gate():
    if not enabled():
        raise LLMUnavailable("AI is switched off in the environment (LLM_ENABLED).")
    if not Firm.get().ai_enabled:
        raise LLMUnavailable("AI features are turned off. The firm owner can turn them on under Settings.")
    prov = provider()
    if not prov:
        raise LLMUnavailable("No AI key is configured. Set OPENROUTER_API_KEY or ANTHROPIC_API_KEY to use this.")
    cap_calls = daily_cap_calls()
    if cap_calls > 0 and calls_today() >= cap_calls:
        log.warning("LLM daily call cap reached (%d)", cap_calls)
        raise LLMUnavailable(f"Today's limit of {cap_calls} AI calls has been reached. It resets at midnight UTC.")
    cap = daily_cap_cents()
    if cap > 0 and spent_today_cents() >= cap:
        log.warning("AI daily spend cap reached (%d cents)", cap)
        raise LLMUnavailable(f"Today's AI budget (${cap / 100:.2f}) has been used. It resets at midnight UTC.")
    return prov


def complete(prompt, system="", max_tokens=800, kind="", entity="", entity_id=None, user_id=None,
             schema=None, temperature=None, effort="low"):
    """Return the model's text. Raises LLMUnavailable (see module docstring). Records an AiRun per network call.

    schema: a JSON Schema dict; the direct path enforces it with structured outputs, OpenRouter gets a prompt line.
    temperature: only sent when given, and only to models that accept it.
    """
    prov = _gate()
    model = model_for(prov)
    prompt = prompt or ""
    if len(prompt) > MAX_CONTEXT_CHARS:
        log.info("llm: prompt of %d chars cut to %d", len(prompt), MAX_CONTEXT_CHARS)
        prompt = prompt[:MAX_CONTEXT_CHARS] + TRUNCATION_NOTE
    if schema and prov == "openrouter":
        prompt += "\n\nReturn only a JSON value matching the requested shape. No prose, no code fences."
    if is_thinking_model(model):
        max_tokens = max(max_tokens, 4096)
    run = AiRun(kind=kind or "complete", entity=entity or "", entity_id=entity_id, model=model,
                prompt_chars=len(prompt) + len(system or ""), output_chars=0, cost_cents=0, ok=False,
                user_id=user_id)
    db.session.add(run)
    db.session.commit()  # reserve the slot before the network call so the cap counts it even if we crash
    try:
        text, in_tok, out_tok = _call_provider(prov, model, prompt, system, max_tokens, schema, temperature, effort)
    except LLMUnavailable:
        db.session.commit()
        raise
    except Exception as exc:  # any transport or parse problem
        log.warning("LLM call failed provider=%s model=%s: %s", prov, model, str(exc)[:200])
        db.session.commit()
        raise LLMUnavailable("The AI service did not answer. Nothing was changed. Try again in a minute.")
    in_tok = in_tok or max(1, run.prompt_chars // 4)
    out_tok = out_tok or max(1, len(text) // 4)
    run.output_chars = len(text)
    run.cost_cents = estimate_cost_cents(model, in_tok, out_tok)
    run.ok = True
    db.session.commit()
    return text


def complete_json(prompt, schema, **kw):
    """complete() plus parsing. Raises LLMBadOutput when the answer is not JSON."""
    raw = complete(prompt, schema=schema, **kw)
    data = _extract_json(raw)
    if data is None:
        log.warning("LLM returned non-JSON for kind=%s: %s", kw.get("kind"), (raw or "")[:200])
        raise LLMBadOutput("The AI answered in an unexpected format. Nothing was changed. Try again.")
    return data


def _call_provider(prov, model, prompt, system, max_tokens, schema, temperature, effort):
    """Returns (text, input_tokens, output_tokens). Raises on any failure (logged by the caller)."""
    if prov == "openrouter":
        return _openrouter(model, prompt, system, max_tokens, temperature, effort)
    return _anthropic(model, prompt, system, max_tokens, schema, temperature, effort)


def _openrouter(model, prompt, system, max_tokens, temperature, effort):
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    payload = {"model": model, "messages": msgs, "max_tokens": max_tokens}
    if temperature is not None and accepts_sampling(model):
        payload["temperature"] = min(float(temperature), 1.0)
    if is_thinking_model(model) and effort:
        payload["reasoning"] = {"effort": effort}
    headers = {"Authorization": f"Bearer {_setting('OPENROUTER_API_KEY')}", "Content-Type": "application/json",
               "X-Title": "Coil"}
    base = _setting("BASE_URL")
    if base:
        headers["HTTP-Referer"] = base
    r = requests.post(OPENROUTER_URL, headers=headers, json=payload, timeout=TIMEOUT)
    if r.status_code >= 400:
        log.warning("OpenRouter %s model=%s body=%s", r.status_code, model, r.text[:200])
        raise LLMUnavailable("The AI service refused the request. The error was logged. Nothing was changed.")
    data = r.json()
    choice = (data.get("choices") or [{}])[0]
    text = ((choice.get("message") or {}).get("content") or "").strip()
    if not text:
        log.warning("OpenRouter empty answer model=%s finish=%s", model, choice.get("finish_reason"))
        raise LLMUnavailable("The AI returned an empty answer. Nothing was changed.")
    usage = data.get("usage") or {}
    return text, int(usage.get("prompt_tokens") or 0), int(usage.get("completion_tokens") or 0)


def _anthropic(model, prompt, system, max_tokens, schema, temperature, effort):
    payload = {"model": model, "max_tokens": max_tokens, "messages": [{"role": "user", "content": prompt}]}
    if system:
        payload["system"] = system
    if temperature is not None and accepts_sampling(model):
        payload["temperature"] = min(float(temperature), 1.0)
    output_config = {}
    if schema:
        output_config["format"] = {"type": "json_schema", "schema": schema}
    if is_thinking_model(model) and effort:
        output_config["effort"] = effort
    if output_config:
        payload["output_config"] = output_config
    r = requests.post(ANTHROPIC_URL, headers={"x-api-key": _setting("ANTHROPIC_API_KEY"),
                                              "anthropic-version": ANTHROPIC_VERSION,
                                              "content-type": "application/json"},
                      json=payload, timeout=TIMEOUT)
    if r.status_code >= 400:
        log.warning("Anthropic %s model=%s body=%s", r.status_code, model, r.text[:200])
        raise LLMUnavailable("The AI service refused the request. The error was logged. Nothing was changed.")
    data = r.json()
    if data.get("stop_reason") == "refusal":
        log.warning("Anthropic refusal model=%s category=%s", model,
                    (data.get("stop_details") or {}).get("category"))
        raise LLMUnavailable("The AI declined to work on this content. Nothing was changed.")
    text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text").strip()
    if not text:
        log.warning("Anthropic empty answer model=%s stop=%s", model, data.get("stop_reason"))
        raise LLMUnavailable("The AI returned an empty answer. Nothing was changed.")
    usage = data.get("usage") or {}
    return text, int(usage.get("input_tokens") or 0), int(usage.get("output_tokens") or 0)


def _extract_json(text):
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except ValueError:
        pass
    for opener, closer in (("[", "]"), ("{", "}")):
        s, e = cleaned.find(opener), cleaned.rfind(closer)
        if s != -1 and e > s:
            try:
                return json.loads(cleaned[s:e + 1])
            except ValueError:
                continue
    return None
