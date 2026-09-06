"""
app/storage — Object storage seam for Flow (FLOW-033).
"""

from app.storage.base import StorageAdapter, get_storage_adapter
from app.storage.local import LocalStorageAdapter
from app.storage.supabase import SupabaseStorageAdapter

__all__ = [
    "LocalStorageAdapter",
    "StorageAdapter",
    "SupabaseStorageAdapter",
    "get_storage_adapter",
]
