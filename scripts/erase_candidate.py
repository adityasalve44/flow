#!/usr/bin/env python
"""
scripts/erase_candidate.py — Manual candidate PII erasure tool (FLOW-044).

For use by the operations/legal team to process GDPR/privacy erasure requests.
Accepts a phone number or candidate UUID, verifies it exists, then calls
erase_candidate_data() with a full audit trail.

Usage:
    python scripts/erase_candidate.py --phone "+91XXXXXXXXXX" \\
        --actor "legal_team" --reason "GDPR request REF-12345"

    python scripts/erase_candidate.py --candidate-id "uuid-here" \\
        --actor "legal_team" --reason "GDPR request REF-12345"

    # Dry-run (print candidate info without erasing):
    python scripts/erase_candidate.py --phone "+91XXXXXXXXXX" --dry-run
"""

import argparse
import json
import logging
import os
import sys
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from sqlalchemy import select

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "message": %(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("flow.erase_candidate")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Flow candidate PII erasure tool.")
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--phone", help="Candidate phone number in E.164 format (e.g. +91XXXXXXXXXX).")
    group.add_argument("--candidate-id", help="Candidate UUID.")
    parser.add_argument("--actor", required=True, help="Who is performing the erasure (e.g. 'legal_team').")
    parser.add_argument("--reason", help="Free-text reason for the erasure (stored in audit log).")
    parser.add_argument("--dry-run", action="store_true", help="Print candidate info without erasing.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from app.db.uow import UnitOfWork
    from app.models.candidate import Candidate
    from app.services.privacy import erase_candidate_data

    with UnitOfWork() as uow:
        # Resolve candidate
        if args.phone:
            stmt = select(Candidate).where(Candidate.phone_number == args.phone)
            cand = uow.session.scalar(stmt)
        else:
            import uuid
            cand = uow.session.get(Candidate, uuid.UUID(args.candidate_id))

        if cand is None:
            logger.error(json.dumps({
                "event": "candidate_not_found",
                "lookup": args.phone or args.candidate_id,
            }))
            print(f"ERROR: Candidate not found: {args.phone or args.candidate_id}", file=sys.stderr)
            return 1

        print("\nCandidate found:")
        print(f"  ID:               {cand.id}")
        print(f"  Phone:            {cand.phone_number}")
        print(f"  Display name:     {cand.display_name}")
        print(f"  Lifecycle status: {cand.lifecycle_status.value}")
        print(f"  Consent status:   {cand.consent_status.value}")
        print(f"  Created:          {cand.created_at}")

        if args.dry_run:
            print("\nDRY-RUN: No changes made.")
            return 0

        # Confirm with user
        confirm = input(f"\nPermanently erase ALL PII for candidate {cand.id}? Type 'yes' to confirm: ").strip()
        if confirm.lower() != "yes":
            print("Aborted.")
            return 0

        actor_id = f"{args.actor}:{args.reason}" if args.reason else args.actor

        logger.info(json.dumps({
            "event": "erasure_start",
            "candidate_id": str(cand.id),
            "actor": actor_id,
            "started_at": datetime.now(UTC).isoformat(),
        }))

        summary = erase_candidate_data(
            uow=uow,
            candidate_id=cand.id,
            actor_type="operator",
            actor_id=actor_id,
        )

    logger.info(json.dumps({
        "event": "erasure_complete",
        "summary": summary,
        "finished_at": datetime.now(UTC).isoformat(),
    }))

    print("\nErasure complete:")
    for k, v in summary.items():
        print(f"  {k}: {v}")
    print(f"\nAudit event recorded. Provide the candidate UUID to the requester as erasure receipt: {summary.get('candidate_id')}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
