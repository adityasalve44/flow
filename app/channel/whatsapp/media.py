"""
app/channel/whatsapp/media.py — Meta Graph API two-step media downloader (FLOW-041).

Implements:
1. Step 1: Query Graph API for temporary media download URL.
2. Step 2: Authenticated fetch of the binary media payload.
3. Pass through app.channel.media.validate_media to enforce security, size, and content safety.
"""

import httpx

from app.channel.media import ValidatedMedia, validate_media
from app.config import get_settings
from app.logging import get_logger

logger = get_logger(__name__)


class WhatsAppMediaDownloadError(Exception):
    """Raised when downloading media from Meta Graph API fails."""


async def download_whatsapp_media(
    media_id: str,
    access_token: str | None = None,
    api_version: str | None = None,
    client: httpx.AsyncClient | None = None,
    override_filename: str | None = None,
) -> ValidatedMedia:
    """
    Download and validate a media attachment via Meta WhatsApp Cloud API.

    Two-step process:
    1. GET https://graph.facebook.com/{version}/{media_id} -> { "url": "..." }
    2. GET {url} with Bearer token -> binary content
    3. Run validate_media on the downloaded bytes.
    """
    settings = get_settings()
    token = access_token or settings.whatsapp_access_token
    version = api_version or settings.whatsapp_api_version

    if not token:
        raise WhatsAppMediaDownloadError("whatsapp_access_token is not configured")

    headers = {"Authorization": f"Bearer {token}"}
    close_client = False
    if client is None:
        client = httpx.AsyncClient(timeout=30.0)
        close_client = True

    try:
        # Step 1: Retrieve media metadata and download URL
        meta_url = f"https://graph.facebook.com/{version}/{media_id}"
        resp_meta = await client.get(meta_url, headers=headers)
        if resp_meta.status_code != 200:
            logger.error(
                "Meta Graph API media metadata lookup failed (status=%d): %s",
                resp_meta.status_code,
                resp_meta.text,
            )
            raise WhatsAppMediaDownloadError(
                f"Failed to lookup media metadata: HTTP {resp_meta.status_code}"
            )

        meta_json = resp_meta.json()
        download_url = meta_json.get("url")
        if not download_url:
            raise WhatsAppMediaDownloadError("No download URL returned by Meta Graph API")

        declared_mime = meta_json.get("mime_type", "application/octet-stream")

        # Step 2: Download the binary file
        resp_binary = await client.get(download_url, headers=headers)
        if resp_binary.status_code != 200:
            logger.error(
                "Meta Graph API media binary download failed (status=%d): %s",
                resp_binary.status_code,
                resp_binary.text,
            )
            raise WhatsAppMediaDownloadError(
                f"Failed to download media binary: HTTP {resp_binary.status_code}"
            )

        binary_data = resp_binary.content

        # Determine filename
        filename = override_filename or meta_json.get("filename")
        if not filename:
            # Fallback based on media_id and mime type
            ext = ".bin"
            if "pdf" in declared_mime:
                ext = ".pdf"
            elif "word" in declared_mime or "docx" in declared_mime:
                ext = ".docx"
            elif "jpeg" in declared_mime or "jpg" in declared_mime:
                ext = ".jpg"
            elif "png" in declared_mime:
                ext = ".png"
            filename = f"whatsapp_{media_id}{ext}"

        # Step 3: Validate through Flow's media safety engine
        validated = validate_media(
            filename=filename,
            content_type=declared_mime,
            data=binary_data,
        )
        return validated

    finally:
        if close_client:
            await client.aclose()
