"""
app/channel package — inbound and outbound messaging channels.
"""

from app.channel.inbound import (
    IngressDecision,
    IngressResult,
    check_rate_limit,
    process_ingress,
)

__all__ = [
    "IngressDecision",
    "IngressResult",
    "check_rate_limit",
    "process_ingress",
]
