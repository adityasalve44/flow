"""
app/observability/cost.py — Token cost accounting for Gemini models (FLOW-040).

Tracks per-model input and output token rates to compute accurate estimated costs per turn.
"""

from typing import NamedTuple


class TokenRates(NamedTuple):
    input_per_million: float
    output_per_million: float


# Pricing in USD per million tokens (standard Gemini Flash tiers)
MODEL_PRICING: dict[str, TokenRates] = {
    "gemini-3.7-flash": TokenRates(input_per_million=0.075, output_per_million=0.30),
    "gemini-3.6-flash": TokenRates(input_per_million=0.075, output_per_million=0.30),
    "gemini-flash-latest": TokenRates(input_per_million=0.075, output_per_million=0.30),
    "gemini-2.5-flash": TokenRates(input_per_million=0.075, output_per_million=0.30),
    "gemini-2.0-flash": TokenRates(input_per_million=0.10, output_per_million=0.40),
    "gemini-1.5-flash": TokenRates(input_per_million=0.075, output_per_million=0.30),
}

DEFAULT_RATES = TokenRates(input_per_million=0.075, output_per_million=0.30)


def estimate_cost(
    model: str | None,
    input_tokens: int = 0,
    output_tokens: int = 0,
) -> float:
    """Calculate estimated cost in USD based on input and output token counts."""
    rates = DEFAULT_RATES
    if model:
        norm_model = model.lower()
        for key, r in MODEL_PRICING.items():
            if key in norm_model:
                rates = r
                break

    cost = (input_tokens / 1_000_000.0) * rates.input_per_million + (
        output_tokens / 1_000_000.0
    ) * rates.output_per_million
    return round(cost, 6)
