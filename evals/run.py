"""
evals/run.py — CLI entrypoint for conversation evaluation runner (FLOW-032).

Usage:
    python -m evals.run

Runs all 23 scenarios from §15 of REVIEW_AND_PLAN.md.
Reports pass/fail per scenario and exits with status 0 if all pass, 1 if any fail.
"""

import asyncio
import sys

from app.database import get_session_factory
from evals.runner import load_scenarios, run_scenario


async def main() -> int:
    session_factory = get_session_factory()
    scenarios = load_scenarios()
    total = len(scenarios)

    print(f"\n{'='*70}")
    print(f"Flow Evaluation Harness — Running {total} scenarios (§15)")
    print(f"{'='*70}\n")

    passed_count = 0
    failed_count = 0

    with session_factory() as session:
        for scen in scenarios:
            scen_id = scen["id"]
            desc = scen["description"]

            try:
                res = await run_scenario(scen, session)
                if res.passed:
                    passed_count += 1
                    status_str = "\033[92mPASS\033[0m"
                    print(f"[{status_str}] #{scen_id:02d}: {desc}")
                else:
                    failed_count += 1
                    status_str = "\033[91mFAIL\033[0m"
                    print(f"[{status_str}] #{scen_id:02d}: {desc}")
                    if res.error:
                        print(f"       Error: {res.error}")
            except Exception as exc:
                failed_count += 1
                status_str = "\033[91mERROR\033[0m"
                print(f"[{status_str}] #{scen_id:02d}: {desc}")
                print(f"       Exception: {exc}")

    print(f"\n{'='*70}")
    print(f"Results: {passed_count}/{total} passed, {failed_count} failed.")
    print(f"{'='*70}\n")

    return 0 if failed_count == 0 else 1


if __name__ == "__main__":
    exit_code = asyncio.run(main())
    sys.exit(exit_code)
