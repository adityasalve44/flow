"""
tests/test_media.py — Media validation and sanitisation tests (FLOW-034).

Validates:
1. Valid PDF, DOCX, image, and text validation.
2. Rejection of .exe disguised as .pdf (PE header MZ).
3. Rejection of oversized files (>10MB).
4. Rejection of empty files (0 bytes).
5. Path traversal and malicious filename sanitization.
6. Checksum stability (deterministic SHA-256).
"""

import hashlib

import pytest

from app.channel.media import (
    MAX_MEDIA_SIZE_BYTES,
    MediaValidationError,
    sanitize_filename,
    validate_media,
)


def test_valid_pdf_validation():
    """Valid PDF document passes validation cleanly."""
    pdf_bytes = b"%PDF-1.5\nSample Resume Content\n%%EOF"
    result = validate_media(
        data=pdf_bytes,
        filename="John_Doe_Resume.pdf",
        content_type="application/pdf",
    )
    assert result.filename == "John_Doe_Resume.pdf"
    assert result.content_type == "application/pdf"
    assert result.extension == ".pdf"
    assert result.size_bytes == len(pdf_bytes)
    assert result.checksum == hashlib.sha256(pdf_bytes).hexdigest()


def test_exe_disguised_as_pdf_rejected():
    """Adversarial check (§16, FLOW-034): A Windows PE executable renamed to .pdf is rejected."""
    # Windows executable starts with 'MZ'
    fake_pdf = b"MZ\x90\x00\x03\x00\x00\x00\x04\x00\x00\x00\xff\xff\x00\x00This is a virus payload"

    with pytest.raises(MediaValidationError) as exc_info:
        validate_media(data=fake_pdf, filename="innocent_resume.pdf")

    assert "executable signature" in str(exc_info.value).lower()
    assert exc_info.value.code == "dangerous_signature"


def test_linux_elf_executable_rejected():
    """Adversarial check: Linux ELF executable is rejected."""
    elf_bytes = b"\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00"
    with pytest.raises(MediaValidationError) as exc_info:
        validate_media(data=elf_bytes, filename="resume.pdf")
    assert exc_info.value.code == "dangerous_signature"


def test_oversized_file_rejected():
    """Files exceeding 10MB limit are rejected before upload."""
    oversized_data = b"%PDF-" + (b"0" * (MAX_MEDIA_SIZE_BYTES + 1024))
    with pytest.raises(MediaValidationError) as exc_info:
        validate_media(data=oversized_data, filename="huge_cv.pdf")

    assert "exceeds maximum allowed limit" in str(exc_info.value)
    assert exc_info.value.code == "oversized_file"


def test_empty_file_rejected():
    """Zero-byte files are rejected."""
    with pytest.raises(MediaValidationError) as exc_info:
        validate_media(data=b"", filename="empty.pdf")
    assert exc_info.value.code == "empty_file"


def test_filename_sanitisation():
    """Path traversal, null bytes, and dangerous characters are sanitized."""
    test_cases = [
        ("../../etc/passwd.pdf", "passwd.pdf"),
        ("..\\..\\windows\\system32\\calc.pdf", "calc.pdf"),
        ("resume\x00_hidden.pdf", "resume_hidden.pdf"),
        ("my;rm -rf /;resume.pdf", "my_rm_-rf_resume.pdf"),
        ("   ", "resume.pdf"),
        (None, "resume.pdf"),
        ("///absolute/path/cv.docx", "cv.docx"),
    ]

    for raw, _expected in test_cases:
        sanitized = sanitize_filename(raw)
        assert ".." not in sanitized
        assert "/" not in sanitized
        assert "\\" not in sanitized
        assert "\x00" not in sanitized
        assert sanitized.endswith((".pdf", ".docx"))


def test_type_mismatch_png_with_pdf_extension():
    """PNG image with .pdf extension triggers content-type mismatch error."""
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    with pytest.raises(MediaValidationError) as exc_info:
        validate_media(data=png_bytes, filename="resume.pdf")

    assert "content-type mismatch" in str(exc_info.value).lower()
    assert exc_info.value.code == "type_mismatch"


def test_valid_image_and_text():
    """Images and text resumes pass with proper extension and content."""
    png_bytes = b"\x89PNG\r\n\x1a\n\x00\x00\x00\rIHDR"
    png_result = validate_media(data=png_bytes, filename="resume_scan.png")
    assert png_result.content_type == "image/png"

    txt_bytes = b"Jane Doe\nSenior Backend Engineer\n5 years experience in Python"
    txt_result = validate_media(data=txt_bytes, filename="resume.txt")
    assert txt_result.content_type == "text/plain"


def test_checksum_deterministic_stability():
    """Checksum is SHA-256 hex string and matches across multiple calls."""
    data = b"%PDF-1.4 sample content for hash test"
    res1 = validate_media(data, "test.pdf")
    res2 = validate_media(data, "test2.pdf")
    assert res1.checksum == res2.checksum
    assert res1.checksum == hashlib.sha256(data).hexdigest()
