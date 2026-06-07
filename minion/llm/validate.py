"""Lightweight API key validation before saving.

test_connection() is blocking — run it inside asyncio.to_thread() or a
Textual worker. It makes the smallest possible authenticated request to
each provider's models endpoint (no token cost for list calls).
"""

from __future__ import annotations


def test_connection(provider_id: str, api_key: str, model_id: str) -> tuple[bool, str]:
    """Validate api_key against the given provider.

    Returns (True, ok_message) or (False, error_message).
    model_id is accepted for future use (e.g. checking model availability)
    but the validation itself only needs the key.
    """
    try:
        if provider_id == "anthropic":
            return _test_anthropic(api_key)
        if provider_id == "openai":
            return _test_openai(api_key)
        if provider_id == "openrouter":
            return _test_openrouter(api_key)
        return False, f"Unknown provider '{provider_id}'"
    except Exception as exc:
        return False, str(exc)


def _test_anthropic(api_key: str) -> tuple[bool, str]:
    import anthropic as _anthropic

    client = _anthropic.Anthropic(api_key=api_key)
    try:
        page = client.models.list(limit=5)
        n = len(list(page.data))
        return True, f"valid · {n} models reachable"
    except _anthropic.AuthenticationError:
        return False, "401 unauthorized — Anthropic rejected this key"
    except _anthropic.APIConnectionError as exc:
        return False, f"connection error — {exc}"


def _test_openai(api_key: str) -> tuple[bool, str]:
    import openai as _openai

    client = _openai.OpenAI(api_key=api_key)
    try:
        models = list(client.models.list())
        return True, f"valid · {len(models)} models reachable"
    except _openai.AuthenticationError:
        return False, "401 unauthorized — OpenAI rejected this key"
    except _openai.APIConnectionError as exc:
        return False, f"connection error — {exc}"


def _test_openrouter(api_key: str) -> tuple[bool, str]:
    import openai as _openai

    client = _openai.OpenAI(
        api_key=api_key,
        base_url="https://openrouter.ai/api/v1",
    )
    try:
        models = list(client.models.list())
        return True, f"valid · {len(models)} models reachable"
    except _openai.AuthenticationError:
        return False, "401 unauthorized — OpenRouter rejected this key"
    except _openai.APIConnectionError as exc:
        return False, f"connection error — {exc}"
