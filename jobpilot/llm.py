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

from openai import OpenAI, RateLimitError
from pydantic import BaseModel, ValidationError

ROOT = Path(__file__).resolve().parent.parent

PROVIDERS = {
    "gemini": {
        "base_url": "https://generativelanguage.googleapis.com/v1beta/openai/",
        "key_env": "GEMINI_API_KEY",
        "default_model": "gemini-3.5-flash",
    },
    "qwen": {
        "base_url": "https://dashscope.aliyuncs.com/compatible-mode/v1",
        "key_env": "DASHSCOPE_API_KEY",
        "default_model": "qwen-plus",
    },
}


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
    client = get_client()
    model = model or default_model()
    schema_json = json.dumps(schema.model_json_schema(), indent=2)
    messages = [
        {"role": "system",
         "content": f"{system}\n\nRespond with ONLY a single JSON object that validates "
                    f"against this JSON schema — no markdown, no commentary:\n{schema_json}"},
        {"role": "user", "content": user},
    ]

    last_err: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            resp = client.chat.completions.create(
                model=model, messages=messages,
                response_format={"type": "json_object"}, max_tokens=max_tokens,
            )
        except RateLimitError as e:
            last_err = e
            time.sleep(min(2 ** attempt * 5, 60) + random.uniform(0, 2))
            continue

        text = resp.choices[0].message.content or ""
        if not text.strip():
            last_err = RuntimeError("empty response (token budget likely consumed by reasoning)")
            max_tokens = min(max_tokens * 2, 32000)
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

    raise RuntimeError(f"Model call failed after {max_retries + 1} attempts: {last_err}")
