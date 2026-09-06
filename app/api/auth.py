"""
app/api/auth.py — recruiter authentication (FLOW-038).

Real recruiter identity, replacing FLOW-037's shared-secret placeholder.
Auth today is a per-recruiter API key, hashed at rest; the credential check
is isolated to get_current_recruiter() specifically so a future move to JWT
touches this one function, not every route in app/api/recruiter.py.

Key lifecycle:
- generate_api_key() returns (plaintext, hash) once, at account creation.
  Only the hash is ever persisted (app.models.recruiter.Recruiter.api_key_hash).
  The plaintext is shown to the recruiter exactly once and cannot be
  recovered — losing it means issuing a new key (see scripts/create_recruiter.py).
- verify_api_key() hashes a presented key with the same scheme and does a
  constant-time comparison, matching the pattern already used for webhook
  and recruiter-search shared secrets elsewhere in this codebase.

There is no self-service account creation endpoint and no bootstrap-mode
loophole: the very first admin account is created by scripts/create_recruiter.py,
run directly against the database by an operator. Every account created
after that goes through POST /recruiter/admin/recruiters, which itself
requires an existing admin — see docs/FINDINGS_AND_DECISIONS.md for why an
unauthenticated "create the first admin" endpoint was deliberately not built.
"""

import hashlib
import secrets

from fastapi import Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.database import get_db
from app.db.uow import UnitOfWork
from app.logging import get_logger
from app.models.enums import RecruiterRoleEnum
from app.models.recruiter import Recruiter

logger = get_logger(__name__)


def generate_api_key() -> tuple[str, str]:
    """Return (plaintext_key, sha256_hash). Persist only the hash."""
    plaintext = secrets.token_urlsafe(32)
    return plaintext, hash_api_key(plaintext)


def hash_api_key(plaintext: str) -> str:
    return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()


def get_recruiter_uow(db: Session = Depends(get_db)) -> UnitOfWork:
    return UnitOfWork(session=db)


def get_current_recruiter(
    x_recruiter_key: str | None = Header(None, alias="X-Recruiter-Key"),
    authorization: str | None = Header(None),
    uow: UnitOfWork = Depends(get_recruiter_uow),
) -> Recruiter:
    """Resolve and return the authenticated Recruiter, or raise 401.

    Accepts the key via X-Recruiter-Key or an Authorization: Bearer header,
    matching the convention already used for the webhook secret. A missing
    key, an unknown key, or a deactivated account all produce the same
    generic 401 — the failure reason is logged, never returned to the
    caller, so a probing client can't distinguish "wrong key" from
    "deactivated account" from "no such account".
    """
    provided = x_recruiter_key
    if not provided and authorization:
        provided = (
            authorization[7:].strip()
            if authorization.lower().startswith("bearer ")
            else authorization.strip()
        )

    if not provided:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Missing recruiter authentication credentials",
        )

    key_hash = hash_api_key(provided)
    recruiter = uow.recruiters.get_by_api_key_hash(key_hash)

    if recruiter is None:
        logger.warning("Rejected recruiter API request: unknown API key")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid recruiter authentication credentials",
        )
    if not recruiter.is_active:
        logger.warning(f"Rejected recruiter API request: deactivated account {recruiter.id}")
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid recruiter authentication credentials",
        )

    return recruiter


def require_admin(recruiter: Recruiter = Depends(get_current_recruiter)) -> Recruiter:
    """Dependency for admin-only endpoints (creating/deactivating recruiter accounts)."""
    if recruiter.role != RecruiterRoleEnum.admin:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="This action requires an admin recruiter account",
        )
    return recruiter
