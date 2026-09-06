"""
app/storage/local.py — Local filesystem storage adapter (FLOW-033).

Provides local file storage for automated tests and offline development:
- Satisfies the two-method StorageAdapter protocol (put, signed_url).
- Isolated per-bucket directories.
- Zero network dependencies.
"""

from datetime import datetime, timezone
from pathlib import Path
import time
import urllib.parse


class LocalStorageAdapter:
    """Local filesystem implementation of StorageAdapter."""

    def __init__(self, base_dir: str | Path = ".storage", bucket: str = "resumes"):
        self.base_dir = Path(base_dir)
        self.bucket = bucket
        self.bucket_dir = self.base_dir / bucket
        self.bucket_dir.mkdir(parents=True, exist_ok=True)

    def put(
        self,
        object_key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """Store bytes to local disk under bucket_dir / object_key."""
        # Clean relative path to avoid path traversal
        clean_key = object_key.lstrip("/").replace("\\", "/")
        dest_path = self.bucket_dir / clean_key
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with open(dest_path, "wb") as f:
            f.write(data)

        # Store simple metadata sidecar if needed
        meta_path = dest_path.with_suffix(dest_path.suffix + ".meta")
        with open(meta_path, "w", encoding="utf-8") as f:
            f.write(f"content_type={content_type}\nsize={len(data)}\n")

        return clean_key

    def signed_url(
        self,
        object_key: str,
        expires_in: int = 3600,
    ) -> str:
        """
        Generate local file signed URL with expiry token.
        Format: file:///absolute/path?token=signed_local&expires_at=...
        """
        clean_key = object_key.lstrip("/").replace("\\", "/")
        dest_path = (self.bucket_dir / clean_key).resolve()
        expires_at = int(time.time()) + expires_in

        # Local pseudo-signature for tests
        sig = f"local_sig_{clean_key}_{expires_at}"
        query = urllib.parse.urlencode({
            "token": sig,
            "expires_at": expires_at,
        })
        return f"file://{dest_path.as_posix()}?{query}"

    def get(self, object_key: str) -> bytes:
        """Read back stored file bytes (test helper)."""
        clean_key = object_key.lstrip("/").replace("\\", "/")
        target_path = self.bucket_dir / clean_key
        if not target_path.exists():
            raise FileNotFoundError(f"Object '{object_key}' does not exist in bucket '{self.bucket}'.")
        with open(target_path, "rb") as f:
            return f.read()

    def exists(self, object_key: str) -> bool:
        """Check if an object exists in the bucket."""
        clean_key = object_key.lstrip("/").replace("\\", "/")
        return (self.bucket_dir / clean_key).exists()

    def delete(self, object_key: str) -> bool:
        """Delete an object from the bucket."""
        clean_key = object_key.lstrip("/").replace("\\", "/")
        target_path = self.bucket_dir / clean_key
        if target_path.exists():
            target_path.unlink()
            meta_path = target_path.with_suffix(target_path.suffix + ".meta")
            if meta_path.exists():
                meta_path.unlink()
            return True
        return False
