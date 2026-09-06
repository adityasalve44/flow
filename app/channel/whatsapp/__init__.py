"""
app/channel/whatsapp — Meta WhatsApp Cloud API channel adapter (FLOW-041).

Invariants:
- Provider-specific code stops at this package.
- InboundEvent DTO is the strict system boundary.
- Nothing under app/domain/, app/services/, or app/agents/ may import from this package.
"""

from app.channel.whatsapp.client import WhatsAppClient
from app.channel.whatsapp.router import router as whatsapp_router

__all__ = ["WhatsAppClient", "whatsapp_router"]
