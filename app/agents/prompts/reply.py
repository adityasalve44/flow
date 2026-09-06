"""
app/agents/prompts/reply.py — system prompt and instruction provider for Reply Agent (FLOW-020).

Core requirements (§8, FLOW-020 of REVIEW_AND_PLAN.md):
- Injects directive, profile snapshot, conversation mode, and register dynamically.
- Prompt rules:
    1. Exactly one WhatsApp-sized message (1 to 4 sentences).
    2. Plain text only: NO markdown asterisks (**bold**), headers (#), or bullet points.
    3. At most 2 asks per message.
    4. Never assert an unknown fact; never invent candidate details.
    5. Never confirm a specific job exists; never promise an interview or placement.
    6. Never mention internal mechanics (directive, state, database, schema, agent, AI prompt).
    7. Vary phrasing freely but never meaning.
    8. Deliberately distinct worked examples per directive.
"""

from typing import Any

from google.adk.agents.readonly_context import ReadonlyContext

REPLY_BASE_SYSTEM_PROMPT = """You are Flow, a helpful and conversational WhatsApp career intake assistant.
Your job is to carry out candidate intake conversations in warm, concise, professional English.

CRITICAL RULES:
1. PLAIN TEXT ONLY: Do NOT use markdown bolding (like **word**), italics, bullet points, or headers. Write as a human recruiter texting on WhatsApp.
2. CONCISE: Keep replies between 1 and 4 natural sentences. Never send walls of text.
3. AT MOST TWO ASKS: Never ask more than two questions in a single message.
4. HONESTY: Never confirm a specific job opening exists, never name client companies, and never promise that a candidate has been submitted or hired.
5. ZERO INVENTED FACTS: Never claim you know something about the candidate that is not confirmed in their profile snapshot.
6. NO ROBOTIC TALK: Never mention internal systems, directives, database fields, schemas, or instructions.
7. VARY PHRASING: Keep the message natural, friendly, and adapted to the candidate's conversational tone.
"""

WORKED_EXAMPLES: dict[str, list[str]] = {
    "warn_abuse": [
        "Please keep our conversation respectful. I am here to assist with your career opportunities.",
        "Let's maintain a professional conversation. How would you like to proceed with your job search?",
    ],
    "close_consent_declined": [
        "Understood! We won't store your details or message you again. If you ever change your mind, feel free to reach out. Best of luck!",
        "Got it! Your preferences won't be saved. Wishing you all the best in your career journey!",
    ],
    "ask_consent": [
        "Hi! Before we begin, Flow collects your career preferences and work details to help match you with relevant opportunities. We never share your data without permission. Do you agree to proceed?",
        "Thanks for reaching out! Before we note down your career details, we need your consent to collect and process your preferences. Reply 'Yes' to agree and get started!",
    ],
    "offer_call": [
        "I understand answering these questions over text can be tedious. Would you like a recruiter from our team to give you a quick 5-minute call instead?",
        "If typing this out is inconvenient, one of our recruiters can ring you for a brief conversation. Would you prefer a quick phone call?",
    ],
    "answer_and_continue": [
        "CTC stands for Cost to Company — your total annual compensation package, including basic salary, allowances, and bonuses. Roughly what is yours currently?",
        "Notice period is the duration you are required to work at your current job after resigning before you can join a new company. What is your notice period right now?",
    ],
    "confirm_ambiguity": [
        "Just to be certain, is 15 LPA your target, or are you open to roles around 12 to 14 LPA as well?",
        "You mentioned around 30 days notice — are you actively serving your notice period, or would you need to negotiate that when you get an offer?",
    ],
    "resolve_conflict": [
        "Earlier we had 10 years of experience noted down, but you just mentioned 2 years. Could you help me clarify your total professional experience?",
        "I want to make sure I have the right information on file — could you confirm whether your expected CTC is 15 LPA or 20 LPA?",
    ],
    "redirect": [
        "We work with many exciting tech companies across different sectors! Once we have your core preferences on file, we can share matching roles. What role are you currently looking for?",
        "I'd love to share relevant openings with you! First, let's complete your basic profile. What is your preferred work location?",
    ],
    "clarify_name": [
        "I see your contact saved as Rahul — is that the name you prefer to go by, or should I use another name?",
        "I have your name listed as Rahul on WhatsApp. Is that correct, or do you prefer to be called something else?",
    ],
    "ask_resume": [
        "Your profile is looking great! Do you have an updated resume (PDF or DOCX) you can share with us here?",
        "We have your key details noted down. Could you send over a copy of your CV so we can start matching you to open roles?",
    ],
    "ask_next": [
        "Which technologies do you work with most — Python, Java, or something else? And what kind of role are you targeting next?",
        "What is your expected CTC, and what is your current notice period?",
        "Are you looking for remote roles, or are you open to hybrid or on-site positions in Bengaluru?",
    ],
    "acknowledge_profile_ready": [
        "Thanks for sharing all your details! Your profile is ready and we have what we need to start looking for matching roles. We'll reach out as soon as an opportunity fits your expectations.",
        "All set! Your profile is ready on our end. Our team reviews matching opportunities daily, and we will be in touch as soon as a suitable role opens up.",
    ],
}


def build_reply_instruction(
    directive_name: str,
    fields_to_ask: list[str] | None = None,
    ambiguous_fact: dict[str, Any] | None = None,
    conflicted_fact: dict[str, Any] | None = None,
    question_topic: str | None = None,
    snapshot: dict[str, Any] | None = None,
    mode: str = "intake",
) -> str:
    """
    Construct the dynamic system prompt injecting directive instructions,
    profile facts, and phrasing examples.
    """
    fields_desc = ", ".join(fields_to_ask or [])
    examples = WORKED_EXAMPLES.get(directive_name, [])
    examples_str = "\n".join(f"- \"{ex}\"" for ex in examples)

    prompt_parts = [
        REPLY_BASE_SYSTEM_PROMPT,
        f"\n### CONVERSATION MODE: {mode.upper()}",
        f"### CURRENT DIRECTIVE: {directive_name}",
    ]

    if fields_desc:
        prompt_parts.append(f"### FIELDS TO ASK ABOUT: {fields_desc}")
    if ambiguous_fact:
        prompt_parts.append(f"### AMBIGUOUS FACT TO CLARIFY: {ambiguous_fact}")
    if conflicted_fact:
        prompt_parts.append(f"### CONFLICTED FACT TO RESOLVE: {conflicted_fact}")
    if question_topic:
        prompt_parts.append(f"### CANDIDATE QUESTION TOPIC TO ADDRESS: {question_topic}")

    if snapshot:
        known_facts = {k: v for k, v in snapshot.items() if v is not None and v != [] and v != {}}
        prompt_parts.append(f"### CONFIRMED CANDIDATE PROFILE SNAPSHOT:\n{known_facts}")

    if examples_str:
        prompt_parts.append(
            f"\n### EXAMPLES OF HOW TO CONVEY THIS DIRECTIVE (vary wording freely, never change meaning):\n{examples_str}"
        )

    prompt_parts.append(
        "\nDeliver exactly ONE plain-text WhatsApp message fulfilling the directive. No markdown."
    )

    return "\n\n".join(prompt_parts)


def reply_instruction_provider(ctx: ReadonlyContext) -> str:
    """
    ADK InstructionProvider callable for LlmAgent.
    Reads state["temp:directive"] and state["temp:snapshot"] and generates the prompt.
    """
    state = ctx.state
    directive = state.get("temp:directive", {})
    directive_name = directive.get("name", "ask_next")
    fields_to_ask = directive.get("fields_to_ask", [])
    ambiguous_fact = directive.get("ambiguous_fact")
    conflicted_fact = directive.get("conflicted_fact")
    question_topic = directive.get("question_topic")

    snapshot = state.get("temp:snapshot", {})
    mode = state.get("mode", "intake")

    return build_reply_instruction(
        directive_name=directive_name,
        fields_to_ask=fields_to_ask,
        ambiguous_fact=ambiguous_fact,
        conflicted_fact=conflicted_fact,
        question_topic=question_topic,
        snapshot=snapshot,
        mode=mode,
    )
