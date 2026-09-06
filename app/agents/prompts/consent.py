"""
app/agents/prompts/consent.py — WhatsApp-shaped consent messages and notices (Q4).

Principles (§3, §8, §19 of REVIEW_AND_PLAN.md):
- Not a legal wall of text.
- Short, WhatsApp-sized message explaining what is collected and why.
- Clear opt-in question.
- Human and warm — not a bot notice.
"""

CONSENT_REQUEST_NOTICE = (
    "Hey! Before we get started, just a quick heads-up — we'll save your career details "
    "to match you with relevant job opportunities. We don't share anything without your go-ahead.\n\n"
    "Cool to proceed?"
)

CONSENT_DECLINED_REPLY = (
    "No worries at all! We won't store anything or reach out again. "
    "Feel free to message if you ever change your mind. All the best!"
)

CONSENT_WITHDRAWN_REPLY = (
    "Got it — we've removed your details. Thanks for letting us know."
)

CONSENT_REASK_NOTICE = (
    "Happy to note down what you've shared! Before we save anything, "
    "we just need your go-ahead to store and use your career details. Okay to continue?"
)
