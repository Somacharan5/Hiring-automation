"""LLM client — provider-agnostic, OpenAI-compatible.

Supported providers (set LLM_PROVIDER in .env):
    gemini  — Google AI Studio free tier   (default)
    qwen    — Alibaba Cloud DashScope

structured_call() enforces a Pydantic schema on the model's JSON output, with
self-correcting retries that feed the validation error back to the model.
"""

from __future__ import annotations

import json
import os
import random
import re
import time
from pathlib import Path

from openai import APIError, OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parent.parent

PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "default_model": "gemini-3.5-flash",
    },
    "deepseek": {
        "base_url": "https://api.deepseek.com",
        "key_env": "DEEPSEEK_API_KEY",
        "default_model": "deepseek-chat",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "default_model": "qwen-plus",
    },
}

# Providers to skip until this unix time — set when one hits its quota/rate limit,
# so the run routes to the fallback instead of re-hammering the exhausted provider.
_COOLDOWN: dict[str, float] = {}


def _provider_chain(primary: str) -> list[str]:
    """Primary provider, then DeepSeek as the paid fallback when Gemini's quota runs out."""
    chain = [primary]
    if primary != "deepseek" and os.environ.get("DEEPSEEK_API_KEY"):
        chain.append("deepseek")
    return chain


def _load_env() -> None:
    env = ROOT / ".env"
    if not env.exists():
        return
    for line in env.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, _, v = line.partition("=")
            os.environ.setdefault(k.strip(), v.strip())


def active_provider() -> str:
    _load_env()
    name = os.environ.get("LLM_PROVIDER", "gemini").lower()
    if name not in PROVIDERS:
        raise RuntimeError(f"Unknown LLM_PROVIDER={name!r}. Use one of: {', '.join(PROVIDERS)}")
    return name


def default_model() -> str:
    return PROVIDERS[active_provider()]["default_model"]


def get_client(provider: str | None = None) -> OpenAI:
    name = provider or active_provider()
    cfg = PROVIDERS[name]
    key = os.environ.get(cfg["key_env"])
    if not key:
        raise RuntimeError(f"{cfg['key_env']} not set — add it to the project .env file")
    base = os.environ.get(f"{name.upper()}_BASE_URL") or cfg["base_url"]
    return OpenAI(api_key=key, base_url=base)


def _extract_json(text: str) -> str:
    """Strip markdown fences / surrounding prose if the model added any."""
    text = text.strip()
    fenced = re.search(r"```(?:json)?\s*(\{.*\})\s*```", text, re.S)
    if fenced:
        return fenced.group(1)
    start, end = text.find("{"), text.rfind("}")
    return text[start:end + 1] if start != -1 and end > start else text


def structured_call(system: str, user: str, schema: type[BaseModel],
                    model: str | None = None, max_retries: int = 2,
                    max_tokens: int = 8000) -> BaseModel:
    """Chat call that must return JSON matching `schema`.

    Retries on schema-validation failure (feeding the error back) and backs off
    on rate limits — Gemini's free tier is requests-per-minute capped.
    `max_tokens` is generous by default: reasoning models spend part of the
    budget on thinking before emitting the answer.
    """
    _load_env()
    primary = active_provider()
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    base = [
        {"role": "system",
         "content": f"{system}\n\nRespond with ONLY a single JSON object that validates "
                    f"against this JSON schema — no markdown, no commentary:\n{schema_json}"},
        {"role": "user", "content": user},
    ]

    last_err: Exception | None = None
    for provider in _provider_chain(primary):
        if _COOLDOWN.get(provider, 0.0) > time.time():
            continue                                   # recently exhausted — skip to the fallback
        try:
            client = get_client(provider)
        except RuntimeError as e:                      # provider not configured (no key)
            last_err = e
            continue
        mdl = (model if provider == primary else None) or PROVIDERS[provider]["default_model"]
        messages = list(base)
        mt = max_tokens
        cooled = False
        for attempt in range(max_retries + 1):
            try:
                resp = client.chat.completions.create(
                    model=mdl, messages=messages,
                    response_format={"type": "json_object"}, max_tokens=mt)
            except RateLimitError as e:
                last_err = e
                if attempt < max_retries:               # transient per-minute cap — back off + retry
                    time.sleep(min(2 ** attempt * 4, 20) + random.uniform(0, 2))
                    continue
                _COOLDOWN[provider] = time.time() + 90  # persistent (quota) — cool down, use fallback
                cooled = True
                break
            except APIError as e:                        # 402/401/5xx — provider unusable right now
                last_err = e
                _COOLDOWN[provider] = time.time() + 90
                cooled = True
                break

            text = resp.choices[0].message.content or ""
            if not text.strip():
                last_err = RuntimeError("empty response (token budget likely consumed by reasoning)")
                mt = min(mt * 2, 32000)
                continue
            try:
                return schema.model_validate_json(_extract_json(text))
            except (ValidationError, json.JSONDecodeError) as e:
                last_err = e
                messages += [
                    {"role": "assistant", "content": text},
                    {"role": "user",
                     "content": f"That JSON failed validation:\n{e}\n\nReply with the corrected JSON object only."},
                ]
        if not cooled:
            break     # provider answered but validation kept failing — don't burn the paid fallback

    raise RuntimeError(f"Model call failed (tried {_provider_chain(primary)}): {last_err}")
