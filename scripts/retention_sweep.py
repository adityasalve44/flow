#!/usr/bin/env python
"""
scripts/retention_sweep.py — Production retention sweep runner (FLOW-044).

Designed to be scheduled as a cron job (e.g. nightly at 02:00).
Reads database configuration from environment (same as the application).

Usage:
    # Dry-run (no changes, prints expected counts):
    python scripts/retention_sweep.py --dry-run

    # Execute sweep:
    python scripts/retention_sweep.py

    # Override retention windows:
    RETENTION_PROTECTED_DAYS=30 python scripts/retention_sweep.py

Cron example (run nightly at 02:00):
    0 2 * * * cd /app && python scripts/retention_sweep.py >> /var/log/flow/retention.log 2>&1
"""

import argparse
import json
import logging

# Make sure we can import from the project root
import os
import sys
from dataclasses import asdict
from datetime import UTC, datetime

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from app.db.uow import UnitOfWork
from app.services.privacy import RetentionConfig, run_retention_sweep

logging.basicConfig(
    level=logging.INFO,
    format='{"time": "%(asctime)s", "level": "%(levelname)s", "logger": "%(name)s", "message": %(message)s}',
    datefmt="%Y-%m-%dT%H:%M:%SZ",
)
logger = logging.getLogger("flow.retention_sweep")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Flow data retention sweep — prune expired candidate data per Q5 data classes."
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Simulate the sweep without modifying the database.",
    )
    parser.add_argument(
        "--protected-days",
        type=int,
        default=None,
        help="Override protected data class retention (days). Default: RETENTION_PROTECTED_DAYS env or 30.",
    )
    parser.add_argument(
        "--personal-days",
        type=int,
        default=None,
        help="Override personal data class retention (days). Default: RETENTION_PERSONAL_DAYS env or 90.",
    )
    parser.add_argument(
        "--operational-days",
        type=int,
        default=None,
        help="Override operational data class retention (days). Default: RETENTION_OPERATIONAL_DAYS env or 365.",
    )
    parser.add_argument(
        "--closed-conversation-days",
        type=int,
        default=None,
        help="Override closed conversation retention (days). Default: RETENTION_CLOSED_CONVERSATION_DAYS env or 180.",
    )
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    from app.config import get_settings
    settings = get_settings()

    config = RetentionConfig(
        protected_days=args.protected_days or settings.retention_protected_days,
        personal_days=args.personal_days or settings.retention_personal_days,
        operational_days=args.operational_days or settings.retention_operational_days,
        closed_conversation_days=args.closed_conversation_days or settings.retention_closed_conversation_days,
    )

    mode = "DRY-RUN" if args.dry_run else "EXECUTE"
    logger.info(
        json.dumps({
            "event": "retention_sweep_start",
            "mode": mode,
            "config": asdict(config),
            "started_at": datetime.now(UTC).isoformat(),
        })
    )

    exit_code = 0
    try:
        with UnitOfWork() as uow:
            result = run_retention_sweep(
                uow=uow,
                retention_config=config,
                dry_run=args.dry_run,
            )
        logger.info(
            json.dumps({
                "event": "retention_sweep_complete",
                "mode": mode,
                "result": asdict(result),
                "finished_at": datetime.now(UTC).isoformat(),
            })
        )
        if not args.dry_run:
            print(
                f"Retention sweep complete: "
                f"protected={result.protected_attributes_pruned} "
                f"personal={result.personal_attributes_pruned} "
                f"operational={result.operational_attributes_pruned} "
                f"conversations={result.conversations_pruned} "
                f"messages={result.messages_pruned}"
            )
        else:
            print(
                f"DRY-RUN: would prune: "
                f"protected={result.protected_attributes_pruned} "
                f"personal={result.personal_attributes_pruned} "
                f"operational={result.operational_attributes_pruned} "
                f"conversations={result.conversations_pruned} "
                f"messages={result.messages_pruned}"
            )
    except Exception as exc:
        logger.error(json.dumps({"event": "retention_sweep_error", "error": str(exc)}))
        exit_code = 1

    return exit_code


if __name__ == "__main__":
    sys.exit(main())
