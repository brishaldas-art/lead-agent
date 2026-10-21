"""Gemini gateway: the only module in the project that talks to the Gemini API."""
import hashlib
import logging
import os
import time
from pathlib import Path
from types import SimpleNamespace

import diskcache
from dotenv import load_dotenv
from google import genai
from google.genai import errors, types
from pydantic import BaseModel

from agent.budget import Budget
from agent.prompts import SYSTEM

load_dotenv()
log = logging.getLogger("agent")

CACHE_DIR = Path("outputs/.llm_cache")
CACHE_DIR.mkdir(parents=True, exist_ok=True)
CACHE = diskcache.Cache(str(CACHE_DIR))
BUDGET = Budget.from_env()

_client = None
_key_warned = False


def _get_client():
    """Create the Gemini client on first use. Returns None if the key is missing."""
    global _client, _key_warned
    if _client is not None:
        return _client
    key = os.getenv("GEMINI_API_KEY", "")
    if not key or key.startswith("paste_"):
        if not _key_warned:
            log.error("GEMINI_API_KEY missing or placeholder in .env")
            _key_warned = True
        return None
    _client = genai.Client(api_key=key)
    return _client


def _chain(tier: str) -> list[str]:
    """Return the model fallback chain for a tier, from .env."""
    var = "MODEL_SMART" if tier == "smart" else "MODEL_FAST"
    return [m.strip() for m in os.getenv(var, "").split(",") if m.strip()]


def _thinking(model: str):
    """Minimal thinking config. Returns None if the SDK rejects it."""
    try:
        if "gemini-3" in model:
            return types.ThinkingConfig(thinking_level="low")
        return types.ThinkingConfig(thinking_budget=0)
    except Exception:
        return None


def _generate(client, model, schema, contents, max_out, thinking):
    config = types.GenerateContentConfig(
        system_instruction=SYSTEM,
        response_mime_type="application/json",
        response_schema=schema,
        max_output_tokens=max_out,
        thinking_config=thinking,
    )
    return client.models.generate_content(model=model, contents=contents, config=config)


def _usage(response):
    usage = getattr(response, "usage_metadata", None)
    if usage is None:
        return SimpleNamespace(prompt_token_count=0, candidates_token_count=0, thoughts_token_count=0)
    return usage


def _parse(response, schema):
    parsed = getattr(response, "parsed", None)
    if isinstance(parsed, schema):
        return parsed
    return schema.model_validate_json(response.text or "")


def _cache_key(tier: str, schema: type[BaseModel], prompt: str) -> str:
    return hashlib.sha256(f"{tier}|{schema.__name__}|{SYSTEM}|{prompt}".encode()).hexdigest()


def ask(
    schema: type[BaseModel],
    prompt: str,
    *,
    tier: str = "fast",
    tag: str = "",
    max_out: int = 800,
    contents_extra: list | None = None,
) -> BaseModel | None:
    """Ask Gemini for a structured answer. Never raises; returns None on failure."""
    try:
        return _ask(schema, prompt, tier, tag, max_out, contents_extra)
    except Exception as exc:
        log.error("Gemini gateway error (%s): %s", tag, type(exc).__name__)
        return None


def _ask(schema, prompt, tier, tag, max_out, contents_extra):
    key = _cache_key(tier, schema, prompt)
    if key in CACHE:
        BUDGET.cache_hits += 1
        return schema.model_validate_json(CACHE[key])

    if os.getenv("OFFLINE") == "1":
        return None

    client = _get_client()
    if client is None:
        return None

    contents = [*(contents_extra or []), prompt]
    est_in = len(prompt) // 4

    for model in _chain(tier):
        if not BUDGET.allow(model, est_in + max_out):
            continue

        for attempt in range(3):
            BUDGET.wait_for_slot(model, est_in + max_out)
            thinking = _thinking(model)
            try:
                try:
                    response = _generate(client, model, schema, contents, max_out, thinking)
                except errors.APIError as e:
                    if thinking is not None and e.code == 400 and "thinking" in str(e).lower():
                        log.warning("thinking config removed for %s", model)
                        response = _generate(client, model, schema, contents, max_out, None)
                    else:
                        raise

                BUDGET.record(model, tag, _usage(response))
                out = _parse(response, schema)
                CACHE[key] = out.model_dump_json()
                return out

            except errors.APIError as e:
                msg = str(e)
                if e.code == 429 and "perday" in msg.lower().replace(" ", ""):
                    BUDGET.mark_exhausted(model)
                    break
                if e.code in (429, 500, 503):
                    time.sleep(4 * 2 ** attempt)
                    continue
                log.error("Gemini %s on %s: %s", e.code, model, msg[:120])
                break

            except ValueError:
                log.warning("parse failure (%s) attempt %d", tag, attempt)
                continue

    log.error("Gemini unavailable for %s; step degrades", tag)
    return None


__all__ = ["ask", "SYSTEM"]