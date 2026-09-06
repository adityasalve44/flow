"""
app/channel/media.py — Media validation and sanitisation engine (FLOW-034).

Core requirements (§8, §16, FLOW-034 of REVIEW_AND_PLAN.md):
- Allow-list content types (PDF, DOCX, DOC, RTF, TXT, JPEG, PNG).
- Hard 10MB size cap.
- Filename sanitisation (reject/clean path traversal, shell chars, null bytes).
- Verify file extension against declared MIME type and actual magic bytes.
- Reject executable disguises (e.g. .exe renamed to .pdf).
- Compute deterministic SHA-256 checksum.
- Extensible hook for antivirus scanning (Phase 6).
"""

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path

# Hard caps and allow-lists
MAX_MEDIA_SIZE_BYTES = 10 * 1024 * 1024  # 10 MB
ALLOWED_MIME_TYPES: dict[str, tuple[str, ...]] = {
    "application/pdf": (".pdf",),
    "application/vnd.openxmlformats-officedocument.wordprocessingml.document": (".docx",),
    "application/msword": (".doc",),
    "application/rtf": (".rtf",),
    "text/plain": (".txt",),
    "image/jpeg": (".jpg", ".jpeg"),
    "image/png": (".png",),
}

# Reverse mapping: Extension -> canonical MIME type
EXTENSION_TO_MIME: dict[str, str] = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".doc": "application/msword",
    ".rtf": "application/rtf",
    ".txt": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
}

# Dangerous executable magic headers to block immediately
DANGEROUS_SIGNATURES: list[tuple[bytes, str]] = [
    (b"MZ", "DOS/Windows executable PE (.exe, .dll)"),
    (b"\x7fELF", "Linux ELF executable"),
    (b"\xca\xfe\xba\xbe", "Java class or Mach-O fat binary"),
    (b"\xfe\xed\xfa\xce", "Mach-O 32-bit"),
    (b"\xfe\xed\xfa\xcf", "Mach-O 64-bit"),
    (b"#!", "Executable script shebang"),
    (b"<?php", "PHP script"),
    (b"<script", "HTML/JS payload"),
]


class MediaValidationError(ValueError):
    """Raised when uploaded media fails safety, size, or format validation."""

    def __init__(self, message: str, code: str = "validation_error"):
        super().__init__(message)
        self.code = code


@dataclass(frozen=True)
class ValidatedMedia:
    """Immutable representation of safe, validated media ready for storage."""

    filename: str
    content_type: str
    extension: str
    size_bytes: int
    checksum: str  # SHA-256 hex string
    data: bytes


def sanitize_filename(raw_filename: str | None, default_ext: str = ".pdf") -> str:
    """
    Sanitize an untrusted user-supplied filename.

    - Strips path traversal sequences (../, ..\\, leading slashes).
    - Removes control characters and null bytes.
    - Limits length to 100 characters.
    - Preserves safe alphanumeric characters, underscores, hyphens, and dot.
    """
    if not raw_filename or not raw_filename.strip():
        return f"resume{default_ext}"

    # Extract base filename ignoring any directory components
    name = Path(raw_filename).name

    # Remove null bytes and path traversal patterns
    name = name.replace("\x00", "").replace("..", "")

    # Replace any character that is not alphanumeric, hyphen, underscore, or period
    name = re.sub(r"[^A-Za-z0-9._-]", "_", name)

    # Collapse multiple dots and underscores
    name = re.sub(r"\.{2,}", ".", name)
    name = re.sub(r"_{2,}", "_", name).strip("._-")

    if not name:
        return f"resume{default_ext}"

    # Ensure max length 100
    stem = Path(name).stem[:80]
    ext = Path(name).suffix.lower()
    return f"{stem}{ext}" if ext else f"{stem}{default_ext}"


def detect_content_type(data: bytes, declared_ext: str | None = None) -> str:
    """
    Detect MIME type by inspecting leading magic bytes.
    Verifies that the content truly matches the expected format.
    """
    if len(data) == 0:
        raise MediaValidationError("File is empty (0 bytes).", code="empty_file")

    # 1. Check for dangerous executable headers
    for sig, desc in DANGEROUS_SIGNATURES:
        if data.startswith(sig):
            raise MediaValidationError(
                f"Malicious file detected: executable signature '{desc}'.",
                code="dangerous_signature",
            )

    # 2. PDF check
    if data.startswith(b"%PDF-"):
        return "application/pdf"

    # 3. PNG check
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"

    # 4. JPEG check
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"

    # 5. RTF check
    if data.startswith(b"{\\rtf"):
        return "application/rtf"

    # 6. DOC (OLE2) check
    if data.startswith(b"\xd0\xcf\x11\xe0\xa1\xb1\x1a\xe1"):
        return "application/msword"

    # 7. DOCX (ZIP archive containing word/)
    if data.startswith(b"PK\x03\x04"):
        # Quick check for DOCX internal structure
        if b"word/" in data[:4096] or b"[Content_Types].xml" in data[:4096]:
            return "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
        raise MediaValidationError(
            "Generic ZIP archive detected without Word document structure.",
            code="invalid_docx",
        )

    # 8. Plain text fallback if requested and clean
    if declared_ext == ".txt":
        try:
            sample = data[:4096].decode("utf-8")
            if "\x00" not in sample:
                return "text/plain"
        except UnicodeDecodeError:
            pass

    raise MediaValidationError(
        "Unsupported or unrecognized file format. Allowed formats: PDF, DOCX, DOC, RTF, TXT, JPEG, PNG.",
        code="unsupported_format",
    )


def scan_media_antivirus(data: bytes, filename: str) -> bool:
    """
    Antivirus scanning hook for Phase 6 production security.
    Returns True if clean, raises MediaValidationError if infected.
    """
    # Phase 6 integration hook: e.g. ClamAV daemon or AWS GuardDuty / VirusTotal API
    return True


def validate_media(
    data: bytes,
    filename: str | None = None,
    content_type: str | None = None,
) -> ValidatedMedia:
    """
    Validate and sanitize an uploaded document or image.

    Validates:
    1. Size within 0 < size <= 10MB.
    2. Header inspection matching declared type (rejects disguised executables).
    3. Filename sanitization against traversal and shell characters.
    4. Computes deterministic SHA-256 checksum.
    5. Phase 6 Antivirus scan hook.
    """
    if not isinstance(data, (bytes, bytearray)):
        raise TypeError(f"Expected bytes data, got {type(data).__name__}")

    data_bytes = bytes(data)

    # 1. Size bounds check
    size = len(data_bytes)
    if size == 0:
        raise MediaValidationError("File is empty (0 bytes).", code="empty_file")
    if size > MAX_MEDIA_SIZE_BYTES:
        raise MediaValidationError(
            f"File size ({size / (1024 * 1024):.1f}MB) exceeds maximum allowed limit of 10MB.",
            code="oversized_file",
        )

    # 2. Filename sanitisation
    safe_filename = sanitize_filename(filename)
    declared_ext = Path(safe_filename).suffix.lower()

    if declared_ext not in EXTENSION_TO_MIME:
        raise MediaValidationError(
            f"Disallowed file extension '{declared_ext}'. Supported: {list(EXTENSION_TO_MIME.keys())}",
            code="disallowed_extension",
        )

    # 3. Magic bytes content-type detection
    detected_mime = detect_content_type(data_bytes, declared_ext=declared_ext)

    # 4. Verify extension matches detected content type
    allowed_extensions = ALLOWED_MIME_TYPES.get(detected_mime, ())
    if declared_ext not in allowed_extensions:
        raise MediaValidationError(
            f"Content-type mismatch: file has extension '{declared_ext}' but content signature is '{detected_mime}'.",
            code="type_mismatch",
        )

    # If caller supplied a declared content_type, verify compatibility
    if content_type:
        clean_declared = content_type.split(";")[0].strip().lower()
        # A declared type that neither matches the sniffed type nor permits
        # this extension is a mismatch; generic octet-stream is always allowed.
        if (
            clean_declared != detected_mime
            and clean_declared
            not in ("application/octet-stream", "binary/octet-stream")
            and declared_ext not in ALLOWED_MIME_TYPES.get(clean_declared, ())
        ):
                raise MediaValidationError(
                    f"Declared MIME type '{clean_declared}' contradicts detected '{detected_mime}'.",
                    code="mime_mismatch",
                )

    # 5. Antivirus scanning hook
    scan_media_antivirus(data_bytes, safe_filename)

    # 6. Compute deterministic checksum
    checksum = hashlib.sha256(data_bytes).hexdigest()

    return ValidatedMedia(
        filename=safe_filename,
        content_type=detected_mime,
        extension=declared_ext,
        size_bytes=size,
        checksum=checksum,
        data=data_bytes,
    )
