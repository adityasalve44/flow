"""
app/agents/policy.py — custom ADK BaseAgent for deterministic policy execution (FLOW-019).

Core responsibilities (§8, FLOW-019 of REVIEW_AND_PLAN.md):
- Custom BaseAgent that makes ZERO model calls.
- Reads temp:extraction from session state.
- Executes validate -> merge -> persist -> projection -> counters -> ladder.
- Yields Event with state_delta carrying:
    "temp:directive": directive dictionary
    "temp:snapshot": ProfileSnapshot dictionary
    "recently_asked": updated list of recent asks
    "declined_keys": updated set/list of declined keys
"""

from collections.abc import AsyncGenerator, Callable
from dataclasses import asdict
from typing import Any

from google.adk.agents import BaseAgent
from google.adk.agents.invocation_context import InvocationContext
from google.adk.events import Event, EventActions
from pydantic import Field
from sqlalchemy.orm import Session, sessionmaker

from app.agents.schemas import TurnExtraction
from app.database import get_session_factory
from app.db.uow import UnitOfWork
from app.domain.policy import evaluate_policy_step


class PolicyAgent(BaseAgent):
    """
    Deterministic policy agent (zero model calls).

    Invariants (§8, FLOW-019):
    - Reads temp:extraction from session state.
    - Updates database and derived projection atomically inside UnitOfWork.
    - Evaluates next-question scoring and the 13-rung priority ladder.
    - Yields Event with temp:directive and temp:snapshot in state_delta.
    """

    session_factory: Any = Field(default=None, exclude=True)

    def __init__(
        self,
        name: str = "policy",
        session_factory: Callable[[], Session] | sessionmaker | None = None,
        **kwargs: Any,
    ) -> None:
        super().__init__(name=name, **kwargs)
        self.session_factory = session_factory

    async def _run_async_impl(
        self, ctx: InvocationContext
    ) -> AsyncGenerator[Event]:
        # 1. Read input state
        state = ctx.session.state
        candidate_id = state.get("candidate_id")
        conversation_id = state.get("conversation_id")
        raw_extraction = state.get("temp:extraction")

        extraction = TurnExtraction.safe_parse(raw_extraction)
        recently_asked: list[str] = list(state.get("recently_asked", []))
        declined_keys: set[str] = set(state.get("declined_keys", []))
        blackout_sentiment = state.get("temp:blackout_sentiment")

        # 2. Prepare UnitOfWork
        sf = self.session_factory or get_session_factory()
        db_session = sf() if callable(sf) else get_session_factory()()
        uow = UnitOfWork(session=db_session)

        # 3. Evaluate policy
        with uow:
            candidate = uow.candidates.get_by_id(candidate_id) if candidate_id else None
            conversation = (
                uow.conversations.get_by_id(conversation_id) if conversation_id else None
            )

            if not candidate or not conversation:
                # Safe fallback if session lacks entity records
                directive_dict = {
                    "name": "ask_next",
                    "fields_to_ask": ["desired_role"],
                    "reason": "Missing candidate or conversation in DB",
                }
                snapshot_dict: dict[str, Any] = {}
            else:
                directive, snapshot = evaluate_policy_step(
                    uow=uow,
                    candidate=candidate,
                    conversation=conversation,
                    extraction=extraction,
                    recently_asked=recently_asked,
                    declined_keys=declined_keys,
                    blackout_sentiment=blackout_sentiment,
                )
                directive_dict = directive.to_dict()
                snapshot_dict = asdict(snapshot)

                # Update recently_asked state
                if directive.fields_to_ask:
                    for f in directive.fields_to_ask:
                        if f not in recently_asked:
                            recently_asked.append(f)
                    recently_asked = recently_asked[-6:]  # Keep last 6 asks

                # Update declined_keys state if candidate refused
                if extraction.refusal_signal and directive.fields_to_ask:
                    declined_keys.update(directive.fields_to_ask)

                uow.conversations.add(conversation)
                uow.commit()

        # 4. Yield Event with state_delta
        yield Event(
            author=self.name,
            actions=EventActions(
                state_delta={
                    "temp:directive": directive_dict,
                    "directive": directive_dict,
                    "temp:snapshot": snapshot_dict,
                    "recently_asked": recently_asked,
                    "declined_keys": list(declined_keys),
                }
            ),
        )
