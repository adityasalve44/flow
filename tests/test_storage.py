"""
tests/test_storage.py — Storage adapter test suite (FLOW-033).

Tests:
1. StorageAdapter protocol conformance (LocalStorageAdapter and SupabaseStorageAdapter).
2. Local filesystem backend: put, get, signed_url, exists, delete.
3. Supabase adapter: unit test with mocked HTTP client verifying headers, endpoints, payloads.
4. Factory resolution based on Settings.
5. Integration test against live Supabase (skipped if env vars absent).
"""

import os
from unittest.mock import MagicMock

import httpx
import pytest

from app.config import Settings
from app.storage import (
    LocalStorageAdapter,
    StorageAdapter,
    SupabaseStorageAdapter,
    get_storage_adapter,
)


def test_local_storage_adapter_protocol_and_crud(tmp_path):
    """Verify LocalStorageAdapter satisfies StorageAdapter protocol and executes CRUD."""
    adapter = LocalStorageAdapter(base_dir=tmp_path, bucket="test-resumes")
    assert isinstance(adapter, StorageAdapter)

    content = b"%PDF-1.4 Mock Resume Document Content"
    key = "candidates/123/resume_v1.pdf"

    # 1. Put
    saved_key = adapter.put(key, content, content_type="application/pdf")
    assert saved_key == key
    assert adapter.exists(key) is True

    # 2. Get
    retrieved = adapter.get(key)
    assert retrieved == content

    # 3. Signed URL
    url = adapter.signed_url(key, expires_in=1800)
    assert url.startswith("file://")
    assert "token=" in url
    assert "expires_at=" in url

    # 4. Delete
    deleted = adapter.delete(key)
    assert deleted is True
    assert adapter.exists(key) is False


def test_supabase_storage_adapter_mocked_http():
    """Verify SupabaseStorageAdapter generates correct API calls and handles signed URLs."""
    mock_client = MagicMock(spec=httpx.Client)

    # Mock put response
    mock_put_response = MagicMock(spec=httpx.Response)
    mock_put_response.status_code = 200
    mock_put_response.text = '{"Key": "resumes/candidate-1/resume.pdf"}'

    # Mock sign response
    mock_sign_response = MagicMock(spec=httpx.Response)
    mock_sign_response.status_code = 200
    mock_sign_response.json.return_value = {
        "signedURL": "/storage/v1/object/sign/resumes/candidate-1/resume.pdf?token=mock_jwt_token"
    }

    mock_client.post.side_effect = [mock_put_response, mock_sign_response]

    adapter = SupabaseStorageAdapter(
        url="https://mock-proj.supabase.co",
        service_role_key="mock-service-key-123",
        bucket="resumes",
        http_client=mock_client,
    )
    assert isinstance(adapter, StorageAdapter)

    # 1. Put
    key = "candidate-1/resume.pdf"
    saved = adapter.put(key, b"PDF bytes", content_type="application/pdf")
    assert saved == key

    put_call = mock_client.post.call_args_list[0]
    assert put_call.args[0] == "https://mock-proj.supabase.co/storage/v1/object/resumes/candidate-1/resume.pdf"
    assert put_call.kwargs["headers"]["Authorization"] == "Bearer mock-service-key-123"
    assert put_call.kwargs["headers"]["x-upsert"] == "true"
    assert put_call.kwargs["content"] == b"PDF bytes"

    # 2. Signed URL
    signed = adapter.signed_url(key, expires_in=3600)
    assert signed == "https://mock-proj.supabase.co/storage/v1/object/sign/resumes/candidate-1/resume.pdf?token=mock_jwt_token"

    sign_call = mock_client.post.call_args_list[1]
    assert sign_call.args[0] == "https://mock-proj.supabase.co/storage/v1/object/sign/resumes/candidate-1/resume.pdf"
    assert sign_call.kwargs["json"] == {"expiresIn": 3600}


def test_factory_selection(tmp_path):
    """Verify get_storage_adapter resolves adapter from settings."""
    # Local default
    s_local = Settings(
        database_url="postgresql+psycopg://user:pass@localhost:5432/flow",
        google_api_key="mock",
        storage_backend="local",
        local_storage_dir=str(tmp_path),
    )
    adapter = get_storage_adapter(s_local)
    assert isinstance(adapter, LocalStorageAdapter)

    # Supabase configured
    s_supa = Settings(
        database_url="postgresql+psycopg://user:pass@localhost:5432/flow",
        google_api_key="mock",
        storage_backend="supabase",
        supabase_url="https://xyz.supabase.co",
        supabase_service_role_key="super-secret-key",
    )
    supa_adapter = get_storage_adapter(s_supa)
    assert isinstance(supa_adapter, SupabaseStorageAdapter)


@pytest.mark.integration
def test_live_supabase_storage_integration():
    """Live integration test against Supabase Storage (skipped if env vars absent)."""
    url = os.getenv("SUPABASE_URL")
    key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not url or not key:
        pytest.skip("SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY not configured in environment.")

    adapter = SupabaseStorageAdapter(
        url=url,
        service_role_key=key,
        bucket="resumes",
    )
    obj_key = f"integration_test_{os.urandom(4).hex()}.txt"
    saved = adapter.put(obj_key, b"Flow Storage Adapter Integration Test", content_type="text/plain")
    assert saved == obj_key

    signed = adapter.signed_url(obj_key, expires_in=300)
    assert signed.startswith(url)
