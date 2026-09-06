"""
app/agents/root.py — Root SequentialAgent pipeline and App construction (FLOW-021).

Core architecture (§8, FLOW-021 of REVIEW_AND_PLAN.md):
- 3-stage SequentialAgent turn pipeline:
    1. Extractor (LlmAgent): untrusted text -> validated TurnExtraction (temp:extraction)
    2. Policy (Custom BaseAgent): validate -> merge -> persist -> projection -> counters -> directive
    3. Replier (LlmAgent): dynamic instruction provider -> one natural WhatsApp message
- Packaged inside an ADK App(name="flow", root_agent=flow_pipeline).
"""

from typing import Any

from google.adk.agents import BaseAgent, SequentialAgent
from google.adk.apps import App

from app.agents.extraction import create_extractor_agent
from app.agents.policy import PolicyAgent
from app.agents.reply import create_reply_agent

FLOW_APP_NAME = "flow"


def create_flow_agent(
    session_factory: Any = None,
    extractor_model: str | None = None,
    replier_model: str | None = None,
    tools: list[Any] | None = None,
    custom_extractor: BaseAgent | None = None,
    custom_policy: BaseAgent | None = None,
    custom_replier: BaseAgent | None = None,
) -> SequentialAgent:
    """
    Construct the 3-stage SequentialAgent pipeline.
    Allows injecting custom/mock sub-agents for testing or instrumentation.
    """
    extractor = custom_extractor or create_extractor_agent(model=extractor_model)
    policy = custom_policy or PolicyAgent(session_factory=session_factory)
    replier = custom_replier or create_reply_agent(model=replier_model, tools=tools)

    return SequentialAgent(
        name="flow_pipeline",
        sub_agents=[extractor, policy, replier],
    )


def create_flow_app(
    session_factory: Any = None,
    extractor_model: str | None = None,
    replier_model: str | None = None,
    tools: list[Any] | None = None,
    app_name: str = FLOW_APP_NAME,
    custom_extractor: BaseAgent | None = None,
    custom_policy: BaseAgent | None = None,
    custom_replier: BaseAgent | None = None,
) -> App:
    """
    Construct the top-level ADK App containing the Flow SequentialAgent.
    """
    root_agent = create_flow_agent(
        session_factory=session_factory,
        extractor_model=extractor_model,
        replier_model=replier_model,
        tools=tools,
        custom_extractor=custom_extractor,
        custom_policy=custom_policy,
        custom_replier=custom_replier,
    )
    return App(name=app_name, root_agent=root_agent)
