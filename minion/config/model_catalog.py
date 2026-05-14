"""Static provider + model metadata catalog.

Update `LAST_UPDATED` and the model entries manually when providers change
their model lines or pricing. Pricing is in USD per million tokens.
"""

from __future__ import annotations

import os

LAST_UPDATED = "2025-05-12"

PROVIDERS: list[dict] = [
    {
        "id":       "anthropic",
        "name":     "Anthropic",
        "mark":     "A",
        "color":    "#DA7756",
        "tagline":  "Claude models · long context, careful reasoning",
        "key_env":  "ANTHROPIC_API_KEY",
        "key_prefix": "sk-ant-",
        "docs_url": "https://platform.claude.com/settings/keys",
        "models": [
            {
                "id":        "claude-opus-4-5",
                "ctx":       200_000,
                "in_price":  15.00,
                "out_price": 75.00,
                "speed":     2,
                "intel":     5,
                "tag":       "best for tough refactors & research",
            },
            {
                "id":        "claude-sonnet-4-6",
                "ctx":       200_000,
                "in_price":  3.00,
                "out_price": 15.00,
                "speed":     4,
                "intel":     4,
                "tag":       "balanced default · everyday coding",
            },
            {
                "id":        "claude-haiku-4-5",
                "ctx":       200_000,
                "in_price":  0.80,
                "out_price": 4.00,
                "speed":     5,
                "intel":     3,
                "tag":       "fast, cheap subagents",
            },
        ],
    },
    {
        "id":       "openai",
        "name":     "OpenAI",
        "mark":     "O",
        "color":    "#74AA9C",
        "tagline":  "GPT family · broad ecosystem, strong tool use",
        "key_env":  "OPENAI_API_KEY",
        "key_prefix": "sk-",
        "docs_url": "https://platform.openai.com/api-keys",
        "models": [
            {
                "id":        "gpt-4o",
                "ctx":       128_000,
                "in_price":  2.50,
                "out_price": 10.00,
                "speed":     4,
                "intel":     4,
                "tag":       "strong all-rounder",
            },
            {
                "id":        "gpt-4o-mini",
                "ctx":       128_000,
                "in_price":  0.15,
                "out_price": 0.60,
                "speed":     5,
                "intel":     3,
                "tag":       "cheap, fast small steps",
            },
            {
                "id":        "o4-mini",
                "ctx":       200_000,
                "in_price":  1.10,
                "out_price": 4.40,
                "speed":     3,
                "intel":     4,
                "tag":       "reasoning · multi-step debug",
            },
        ],
    },
    {
        "id":       "openrouter",
        "name":     "OpenRouter",
        "mark":     "R",
        "color":    "#9B8FD4",
        "tagline":  "Multi-provider gateway · 100+ models, one key",
        "key_env":  "OPENROUTER_API_KEY",
        "key_prefix": "sk-or-",
        "docs_url": "https://openrouter.ai/keys",
        "models": [
            {
                "id":        "anthropic/claude-sonnet-4-6",
                "ctx":       200_000,
                "in_price":  3.00,
                "out_price": 15.00,
                "speed":     4,
                "intel":     4,
                "tag":       "Claude via OpenRouter",
            },
            {
                "id":        "openai/gpt-4o",
                "ctx":       128_000,
                "in_price":  2.50,
                "out_price": 10.00,
                "speed":     4,
                "intel":     4,
                "tag":       "GPT-4o via OpenRouter",
            },
            {
                "id":        "google/gemini-2.5-pro",
                "ctx":       1_000_000,
                "in_price":  1.25,
                "out_price": 5.00,
                "speed":     3,
                "intel":     5,
                "tag":       "Gemini via OpenRouter",
            },
        ],
    },
]

PROVIDER_MAP: dict[str, dict] = {p["id"]: p for p in PROVIDERS}


def get_provider(provider_id: str) -> dict | None:
    return PROVIDER_MAP.get(provider_id)


def get_model(provider_id: str, model_id: str) -> dict | None:
    p = PROVIDER_MAP.get(provider_id)
    if p is None:
        return None
    return next((m for m in p["models"] if m["id"] == model_id), None)


def has_key(provider_id: str) -> bool:
    p = PROVIDER_MAP.get(provider_id)
    if p is None:
        return False
    return bool(os.getenv(p["key_env"], "").strip())


def fmt_ctx(n: int) -> str:
    if n >= 1_000_000:
        return f"{n / 1_000_000:.0f}M"
    if n >= 1_000:
        return f"{n // 1_000}K"
    return str(n)


def fmt_price(n: float) -> str:
    if n == 0:
        return "free"
    return f"${n:.2f}"
