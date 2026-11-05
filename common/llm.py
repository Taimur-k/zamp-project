"""Thin, optional Claude wrapper.

Every app works without an API key: each LLM step has a deterministic
fallback. When ANTHROPIC_API_KEY is set (env or a .env file at the repo root),
the LLM path is used and the run log says so — the UI shows which engine made
each decision.
"""
from __future__ import annotations

import base64
import json
import os
from pathlib import Path

try:
    from dotenv import load_dotenv

    load_dotenv(Path(__file__).resolve().parent.parent / ".env")
except Exception:  # dotenv is optional
    pass

MODEL = os.environ.get("LLM_MODEL", "claude-opus-5-5")
_client = None


def available() -> bool:
    if os.environ.get("DISABLE_LLM") == "1":
        return False
    if not os.environ.get("ANTHROPIC_API_KEY"):
        return False
    try:
        import anthropic  # noqa: F401
    except ImportError:
        return False
    return True


def engine_name() -> str:
    return f"Claude ({MODEL})" if available() else "Rules engine (no API key)"


def _get_client():
    global _client
    if _client is None:
        import anthropic

        _client = anthropic.Anthropic()
    return _client


def _strictify(schema: dict) -> dict:
    """Structured outputs require additionalProperties:false + all props required on every object."""
    if isinstance(schema, dict):
        if schema.get("type") == "object" and "properties" in schema:
            schema.setdefault("additionalProperties", False)
            schema.setdefault("required", list(schema["properties"].keys()))
        for v in schema.values():
            if isinstance(v, dict):
                _strictify(v)
            elif isinstance(v, list):
                for i in v:
                    _strictify(i)
    return schema


def complete_json(
    system: str,
    prompt: str,
    schema: dict,
    *,
    pdf_bytes: bytes | None = None,
    image_bytes: bytes | None = None,
    image_media_type: str = "image/png",
    effort: str = "low",
    max_tokens: int = 16000,
) -> dict:
    """Single Claude call returning JSON that validates against `schema`.

    Raises on any failure; callers catch and fall back to rules.
    """
    content: list[dict] = []
    if pdf_bytes:
        content.append(
            {
                "type": "document",
                "source": {"type": "base64", "media_type": "application/pdf", "data": base64.b64encode(pdf_bytes).decode()},
            }
        )
    if image_bytes:
        content.append(
            {
                "type": "image",
                "source": {"type": "base64", "media_type": image_media_type, "data": base64.b64encode(image_bytes).decode()},
            }
        )
    content.append({"type": "text", "text": prompt})

    client = _get_client()
    kwargs = dict(
        model=MODEL,
        max_tokens=max_tokens,
        system=system,
        messages=[{"role": "user", "content": content}],
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": _strictify(json.loads(json.dumps(schema)))}},
    )
    try:
        # Server-side fallback: if the primary model declines, the API reroutes by refusal category.
        resp = client.beta.messages.create(betas=["server-side-fallback-2026-07-01"], fallbacks="default", **kwargs)
    except Exception:
        resp = client.messages.create(**kwargs)

    if resp.stop_reason == "refusal":
        raise RuntimeError("Model declined the request")
    if resp.stop_reason == "max_tokens":
        raise RuntimeError("Model output truncated")
    text = next(b.text for b in resp.content if b.type == "text")
    return json.loads(text)
