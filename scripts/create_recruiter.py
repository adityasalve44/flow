#!/usr/bin/env python
"""
scripts/create_recruiter.py — bootstrap a recruiter account (FLOW-038).

There is no self-service "create the first admin" HTTP endpoint by design:
POST /recruiter/admin/recruiters requires an existing admin, which is
exactly the chicken-and-egg problem this script exists to break. Run it
directly against the database as an operator action, not through the API.

Usage:
    python -m scripts.create_recruiter --email you@company.com \
        --name "Your Name" --role admin

Prints the plaintext API key exactly once. It is not stored anywhere and
cannot be recovered — if it's lost, run this again with --role to issue a
fresh account, or add a rotate-key path when FLOW-038's scope grows to need
one (not needed for the first account).
"""

import argparse
import sys

from app.api.auth import generate_api_key
from app.db.uow import UnitOfWork
from app.models.enums import RecruiterRoleEnum
from app.models.recruiter import Recruiter


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", required=True, dest="display_name")
    parser.add_argument(
        "--role", choices=[r.value for r in RecruiterRoleEnum], default="admin"
    )
    args = parser.parse_args()

    plaintext, key_hash = generate_api_key()

    with UnitOfWork() as uow:
        if uow.recruiters.get_by_email(args.email) is not None:
            print(f"error: a recruiter with email '{args.email}' already exists", file=sys.stderr)
            return 1

        recruiter = Recruiter(
            email=args.email,
            display_name=args.display_name,
            role=RecruiterRoleEnum(args.role),
            api_key_hash=key_hash,
        )
        uow.recruiters.add(recruiter)

    print("Recruiter account created.")
    print(f"  id:      {recruiter.id}")
    print(f"  email:   {recruiter.email}")
    print(f"  role:    {recruiter.role.value}")
    print()
    print(f"  API key: {plaintext}")
    print()
    print("This key is shown once and is not recoverable. Store it securely.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
