"""
app/agents/models.py — LLM provider resolution.

Single switch point between model providers. Every agent builder asks this
module for its model instead of reading a model name off settings directly,
so flipping ``LLM_PROVIDER`` (env) swaps the whole pipeline:

    LLM_PROVIDER=gemini   → bare model string, handled natively by google-genai
    LLM_PROVIDER=groq     → google.adk.models.lite_llm.LiteLlm("groq/<model>")

``resolve_model()`` returns whatever ADK's ``LlmAgent(model=...)`` accepts:
either a ``str`` (Gemini) or a ``BaseLlm`` instance (Groq/LiteLLM).

Adding another provider = one more branch here + its fields in app/config.py.
Nothing else in the codebase needs to change.
"""

from __future__ import annotations

from typing import Any, Literal

from app.config import Settings, get_settings
from app.logging import get_logger

logger = get_logger(__name__)

ModelRole = Literal["extractor", "replier"]


def resolve_model(role: ModelRole, override: Any = None) -> Any:
    """Return the model for ``role`` under the configured provider.

    ``override`` (an explicit model string or BaseLlm passed by a caller/test)
    always wins and is returned untouched.
    """
    if override is not None:
        return override

    settings = get_settings()
    provider = settings.llm_provider  # already normalised by the config validator

    if provider == "gemini":
        return _gemini_model(role, settings)
    if provider == "groq":
        return _groq_model(role, settings)

    # Unreachable: the config validator rejects anything else.
    raise ValueError(f"Unsupported llm_provider: {provider!r}")


def _gemini_model(role: ModelRole, settings: Settings) -> str:
    return settings.extractor_model if role == "extractor" else settings.replier_model


def _groq_model(role: ModelRole, settings: Settings) -> Any:
    try:
        from google.adk.models.lite_llm import LiteLlm
    except ImportError as exc:  # pragma: no cover - depends on optional extra
        raise RuntimeError(
            "LLM_PROVIDER=groq needs LiteLLM. Install it with:\n"
            "    pip install 'litellm>=1.0'\n"
            "(or: pip install 'google-adk[extensions]')"
        ) from exc

    name = settings.groq_extractor_model if role == "extractor" else settings.groq_replier_model
    kwargs: dict[str, Any] = {
        # Groq's reasoning models (gpt-oss, qwen3) otherwise return a
        # `reasoning_content` field that (a) leaks into the reply and (b) is
        # rejected by Groq when ADK replays the turn in history. "hidden" drops
        # it entirely. drop_params keeps this harmless for non-reasoning models.
        "reasoning_format": "hidden",
        "drop_params": True,
    }
    if settings.groq_api_key:
        kwargs["api_key"] = settings.groq_api_key

    logger.info("Resolved %s model via Groq/LiteLLM: %s", role, name)
    return LiteLlm(model=f"groq/{name}", **kwargs)
