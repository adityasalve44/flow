"""
app/domain package — pure business logic (no ORM, no HTTP, no models).
"""

from app.domain.merge import (
    Fact,
    MergeContext,
    MergeResult,
    SOURCE_RANKS,
    merge_facts,
)
from app.domain.normalize import (
    ExperienceNormalized,
    LocationNormalized,
    MoneyNormalized,
    NoticePeriodNormalized,
    has_hedge_word,
    normalize_experience,
    normalize_location,
    normalize_money,
    normalize_notice_period,
    normalize_phone,
)
from app.domain.projection import (
    BLOCKING_KEYS,
    ProfileSnapshot,
    rebuild_projection,
)
from app.domain.registry import (
    KEY_REGISTRY,
    KeySpec,
    get_blocking_keys,
    get_data_class,
    get_key_spec,
    is_blocking,
)

__all__ = [
    # Merge engine
    "Fact",
    "MergeContext",
    "MergeResult",
    "SOURCE_RANKS",
    "merge_facts",
    # Normalisers
    "ExperienceNormalized",
    "LocationNormalized",
    "MoneyNormalized",
    "NoticePeriodNormalized",
    "has_hedge_word",
    "normalize_experience",
    "normalize_location",
    "normalize_money",
    "normalize_notice_period",
    "normalize_phone",
    # Projection
    "BLOCKING_KEYS",
    "ProfileSnapshot",
    "rebuild_projection",
    # Key Registry
    "KEY_REGISTRY",
    "KeySpec",
    "get_blocking_keys",
    "get_data_class",
    "get_key_spec",
    "is_blocking",
]
