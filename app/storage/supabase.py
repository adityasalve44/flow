"""
app/storage/supabase.py — Supabase object storage adapter (FLOW-033).

Interacts with Supabase Storage REST API using httpx:
- `put`: Uploads raw object bytes with x-upsert header.
- `signed_url`: Generates short-TTL signed URLs for secure recruiter/agent downloads.
- Accepts an optional httpx.Client or transport for mockable testing.
"""

import urllib.parse

import httpx


class SupabaseStorageAdapter:
    """Supabase object storage implementation of StorageAdapter."""

    def __init__(
        self,
        url: str,
        service_role_key: str,
        bucket: str = "resumes",
        http_client: httpx.Client | None = None,
    ):
        self.base_url = url.rstrip("/")
        self.service_role_key = service_role_key
        self.bucket = bucket
        self.http_client = http_client or httpx.Client(timeout=15.0)

    def _headers(self, content_type: str = "application/json") -> dict[str, str]:
        return {
            "Authorization": f"Bearer {self.service_role_key}",
            "apikey": self.service_role_key,
            "Content-Type": content_type,
        }

    def put(
        self,
        object_key: str,
        data: bytes,
        content_type: str = "application/octet-stream",
    ) -> str:
        """
        Upload raw object bytes to Supabase private storage bucket.
        Uses x-upsert: true so replacements succeed cleanly.
        """
        clean_key = object_key.lstrip("/")
        endpoint = f"{self.base_url}/storage/v1/object/{self.bucket}/{clean_key}"

        headers = self._headers(content_type=content_type)
        headers["x-upsert"] = "true"

        response = self.http_client.post(
            endpoint,
            headers=headers,
            content=data,
        )

        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Supabase storage upload failed ({response.status_code}): {response.text}"
            )

        return clean_key

    def signed_url(
        self,
        object_key: str,
        expires_in: int = 3600,
    ) -> str:
        """
        Generate short-TTL signed download URL from Supabase Storage.
        """
        clean_key = object_key.lstrip("/")
        endpoint = f"{self.base_url}/storage/v1/object/sign/{self.bucket}/{clean_key}"

        headers = self._headers(content_type="application/json")
        payload = {"expiresIn": expires_in}

        response = self.http_client.post(
            endpoint,
            headers=headers,
            json=payload,
        )

        if response.status_code not in (200, 201):
            raise RuntimeError(
                f"Supabase storage sign failed ({response.status_code}): {response.text}"
            )

        data = response.json()
        signed_path = data.get("signedURL") or data.get("signedUrl")
        if not signed_path:
            raise ValueError(f"No signedURL returned by Supabase: {data}")

        # Supabase returns relative path like '/storage/v1/object/sign/resumes/...'
        if signed_path.startswith("http://") or signed_path.startswith("https://"):
            return signed_path

        return urllib.parse.urljoin(f"{self.base_url}/", signed_path.lstrip("/"))

    def delete(self, object_key: str) -> bool:
        """
        Delete an object from Supabase Storage.
        Returns True if deleted or already absent.
        """
        clean_key = object_key.lstrip("/")
        endpoint = f"{self.base_url}/storage/v1/object/{self.bucket}/{clean_key}"
        headers = self._headers(content_type="application/json")

        response = self.http_client.delete(endpoint, headers=headers)
        return response.status_code in (200, 204, 404)
