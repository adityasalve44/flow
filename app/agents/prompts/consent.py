"""
app/agents/prompts/consent.py — WhatsApp-shaped consent messages and notices (Q4).

Principles (§3, §8, §19 of REVIEW_AND_PLAN.md):
- Not a legal wall of text.
- Short, WhatsApp-sized message explaining what is collected and why.
- Clear opt-in question.
"""

CONSENT_REQUEST_NOTICE = (
    "Hi! Before we begin, Flow collects your career preferences and work details "
    "to help match you with relevant job opportunities. We never share your data "
    "without your permission.\n\n"
    "Do you consent to proceed? (Reply 'Yes' to agree)"
)

CONSENT_DECLINED_REPLY = (
    "Understood! We won't store any of your details or reach out again. "
    "If you ever change your mind, just send us a message. Wishing you the best!"
)

CONSENT_WITHDRAWN_REPLY = (
    "Your consent has been withdrawn. We have stopped processing your profile. "
    "Thank you for letting us know."
)

CONSENT_REASK_NOTICE = (
    "Thanks for sharing! Before we can save your preferences and help match you with roles, "
    "we need your consent to collect and process your career details. Do you agree to proceed?"
)
