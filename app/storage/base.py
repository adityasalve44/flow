"""
app/storage/base.py — Storage adapter protocol and factory (FLOW-033).

Core requirements (§8, FLOW-033 of REVIEW_AND_PLAN.md):
- One small seam between Flow and object storage.
- A two-method protocol: `put` and `signed_url`.
- Supabase implementation against a private bucket.
- Local filesystem implementation for tests and development.
- Configuration through settings. Nothing more — no registry, no plugin system.
"""

from typing import Protocol, runtime_checkable

from app.config import Settings, get_settings


@runtime_checkable
class StorageAdapter(Protocol):
    """Two-method protocol for resume and object storage."""

    def put(
        self,
        object_key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """
        Upload raw data to object storage.
        Returns the canonical object key.
        """
        ...

    def signed_url(
        self,
        object_key: str,
        expires_in: int = 3600,
    ) -> str:
        """
        Generate a short-TTL signed URL for secure file retrieval.
        Default expiration is 3600 seconds (1 hour).
        """
        ...


def get_storage_adapter(settings: Settings | None = None) -> StorageAdapter:
    """Factory returning the configured storage adapter (Supabase or Local)."""
    s = settings or get_settings()

    if s.storage_backend == "supabase" and s.supabase_url and s.supabase_service_role_key:
        from app.storage.supabase import SupabaseStorageAdapter

        return SupabaseStorageAdapter(
            url=s.supabase_url,
            service_role_key=s.supabase_service_role_key,
            bucket=s.supabase_resume_bucket,
        )

    from app.storage.local import LocalStorageAdapter

    return LocalStorageAdapter(
        base_dir=s.local_storage_dir,
        bucket=s.supabase_resume_bucket,
    )
