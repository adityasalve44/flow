"""
app/tools/glossary.py — Authoritative recruitment glossary tool (FLOW-022).

Core requirements (§8, FLOW-022 of REVIEW_AND_PLAN.md):
- explain_recruitment_term(term: str) -> dict[str, Any]
- Reads over a static glossary dictionary with concise 3-5 line explanations.
- Returns found=False when the term is not in the glossary (never improvises).
- Takes NO identity parameters (no candidate_id, no phone_number).
"""

from typing import Any

GLOSSARY_DEFINITIONS: dict[str, str] = {
    "ctc": (
        "Cost to Company (CTC) is the total gross annual expenditure incurred by an employer on an employee.\n"
        "It includes fixed base pay, allowances (HRA, travel, special), employer provident fund (PF) and gratuity contributions,\n"
        "as well as performance-linked variable pay or annual bonuses.\n"
        "Net take-home salary is lower than CTC due to statutory tax deductions and retirement withholdings."
    ),
    "notice_period": (
        "The contractual duration an employee must continue working after submitting formal resignation.\n"
        "Typical notice periods in tech and corporate roles range from 15 to 90 days depending on seniority.\n"
        "This transition period enables handover of active duties, documentation, and replacement hiring.\n"
        "Early release may be requested subject to company discretion or notice buyout."
    ),
    "buyout": (
        "Notice period buyout occurs when a new hiring employer compensates the candidate's current employer\n"
        "for the remaining unserved portion of their contractual notice period.\n"
        "The buyout fee typically equals the employee's basic salary for the unserved duration.\n"
        "Both the current employer and the hiring firm must formally agree to the buyout."
    ),
    "garden_leave": (
        "A contractual arrangement where an employee serving notice remains on full pay and benefits\n"
        "but is instructed to stay away from the workplace and cease all active work duties.\n"
        "It is commonly used for senior or client-facing roles to protect proprietary information,\n"
        "client relationships, and intellectual property from immediate competitor access."
    ),
    "fixed_vs_variable": (
        "Fixed pay is the guaranteed, non-contingent salary paid out in equal monthly installments.\n"
        "Variable pay refers to incentive, performance bonus, or commission amounts paid out periodically\n"
        "contingent upon achieving individual, team, or overall company performance milestones.\n"
        "A typical compensation package balances predictable fixed income with variable upside."
    ),
    "esop": (
        "Employee Stock Ownership Plan (ESOP) grants employees the contractual option to purchase company shares\n"
        "at a predetermined exercise or strike price after satisfying time-based or milestone vesting schedules.\n"
        "Standard industry vesting schedules typically span four years with a one-year initial cliff.\n"
        "ESOPs allow candidates to participate directly in the company's long-term capital appreciation."
    ),
    "pip": (
        "A Performance Improvement Plan (PIP) is a structured HR-monitored program designed to support\n"
        "an employee whose performance has fallen below expected standards.\n"
        "The plan defines specific measurable targets, checkpoints, and resources across a fixed period (usually 30–90 days).\n"
        "Successful completion confirms retention; failure to meet targets may lead to role reassignment or separation."
    ),
    "bgv": (
        "Background Verification (BGV) is the compliance validation performed by employers or specialized agencies.\n"
        "It verifies educational credentials, prior employment dates, remuneration slips, criminal records, and address details.\n"
        "Formal offer letters or continued employment are typically contingent on clearing background checks.\n"
        "Any intentional misrepresentation during the hiring process can result in immediate termination."
    ),
    "probation": (
        "An initial trial period (typically 3 to 6 months) at the beginning of employment.\n"
        "During probation, both employer and employee assess mutual fit, performance, and cultural alignment.\n"
        "Notice periods during probation are often shorter (e.g. 15 to 30 days) than confirmed employment.\n"
        "Upon successful completion and evaluation, employment status is formally confirmed in writing."
    ),
    "immediate_joiner": (
        "A candidate who is available to commence employment immediately without serving a future notice period.\n"
        "This applies to candidates currently between jobs, serving their final days of notice, or with confirmed buyout.\n"
        "Immediate availability significantly accelerates the hiring pipeline for time-critical openings."
    ),
    "work_mode": (
        "The operational arrangement governing where an employee performs their daily duties.\n"
        "Common models are on-site (working from company office), remote (working from home or any location),\n"
        "and hybrid (combining dedicated in-office days with remote work flexibility)."
    ),
    "gratuity": (
        "A statutory retirement benefit paid by an employer to an employee for dedicated long-term service.\n"
        "Under labour laws in India and other jurisdictions, it is payable after completing 5 or more continuous years.\n"
        "The calculation is tied to the employee's last drawn basic salary and total completed years of tenure."
    ),
    "relocation_allowance": (
        "Financial compensation or direct expense reimbursement provided to assist an employee moving cities for work.\n"
        "It typically covers packing and moving services, temporary accommodation (usually 14 to 30 days), and transit flights.\n"
        "Relocation packages are usually subject to a minimum retention agreement (typically 12 months)."
    ),
    "joining_bonus": (
        "A one-time lump-sum incentive paid upon beginning employment to attract high-demand talent.\n"
        "It can offset compensation lost from forfeiting unvested stock options or unserved bonuses at a previous employer.\n"
        "It usually carries a 1-year clawback clause requiring repayment if the employee departs early."
    ),
    "retention_bonus": (
        "A financial incentive offered to retain critical employees during key milestones or company transitions.\n"
        "Payment is contingent on the employee remaining actively employed through a specified vesting date.\n"
        "If the employee resigns before the milestone date, the bonus is forfeited or subject to clawback."
    ),
    "hra": (
        "House Rent Allowance (HRA) is a dedicated component of an employee's salary designed to offset rented living costs.\n"
        "Under tax regulations, employees paying rent for residential accommodation can claim statutory income tax exemption.\n"
        "The exemption is calculated based on rent paid, basic salary, and whether the city is a metropolitan area."
    ),
    "provident_fund": (
        "A statutory retirement savings scheme where employer and employee make matching monthly contributions.\n"
        "In India, Employees' Provident Fund (EPF) typically deducts 12% of basic salary towards the retirement corpus.\n"
        "The accumulated balance earns government-declared annual interest and is withdrawable upon retirement or unemployment."
    ),
    "clawback": (
        "A contractual provision entitling an employer to recover bonuses, relocation expenses, or buyout costs.\n"
        "Clawbacks are triggered if the employee voluntarily resigns or is terminated for cause within a designated period (often 12 months).\n"
        "The obligation to repay is legally enforceable under employment contract terms."
    ),
    "take_home_salary": (
        "Net take-home pay is the actual disposable income credited to an employee's bank account each pay cycle.\n"
        "It equals gross salary minus all statutory and voluntary withholdings including income tax (TDS), PF, and professional tax.\n"
        "Because CTC includes employer contributions and variable bonuses, monthly take-home is distinctly lower than CTC/12."
    ),
}

# Synonym mapping to canonical glossary keys
TERM_ALIASES: dict[str, str] = {
    "ctc": "ctc",
    "cost to company": "ctc",
    "annual ctc": "ctc",
    "gross salary": "ctc",
    "notice": "notice_period",
    "notice period": "notice_period",
    "notice_period": "notice_period",
    "serving notice": "notice_period",
    "buyout": "buyout",
    "buy out": "buyout",
    "notice buyout": "buyout",
    "notice period buyout": "buyout",
    "garden leave": "garden_leave",
    "garden_leave": "garden_leave",
    "fixed vs variable": "fixed_vs_variable",
    "fixed pay": "fixed_vs_variable",
    "variable pay": "fixed_vs_variable",
    "fixed and variable": "fixed_vs_variable",
    "base salary": "fixed_vs_variable",
    "esop": "esop",
    "esops": "esop",
    "equity": "esop",
    "stock options": "esop",
    "stocks": "esop",
    "pip": "pip",
    "performance improvement plan": "pip",
    "bgv": "bgv",
    "background verification": "bgv",
    "background check": "bgv",
    "probation": "probation",
    "probation period": "probation",
    "immediate joiner": "immediate_joiner",
    "immediate_joiner": "immediate_joiner",
    "zero notice": "immediate_joiner",
    "work mode": "work_mode",
    "work_mode": "work_mode",
    "hybrid": "work_mode",
    "remote": "work_mode",
    "gratuity": "gratuity",
    "relocation": "relocation_allowance",
    "relocation allowance": "relocation_allowance",
    "relocation_allowance": "relocation_allowance",
    "joining bonus": "joining_bonus",
    "signing bonus": "joining_bonus",
    "joining_bonus": "joining_bonus",
    "sign on bonus": "joining_bonus",
    "retention bonus": "retention_bonus",
    "retention_bonus": "retention_bonus",
    "hra": "hra",
    "house rent allowance": "hra",
    "pf": "provident_fund",
    "epf": "provident_fund",
    "provident fund": "provident_fund",
    "provident_fund": "provident_fund",
    "clawback": "clawback",
    "claw back": "clawback",
    "clawback clause": "clawback",
    "take home": "take_home_salary",
    "take home salary": "take_home_salary",
    "in hand": "take_home_salary",
    "in hand salary": "take_home_salary",
    "net pay": "take_home_salary",
    "net salary": "take_home_salary",
}


def explain_recruitment_term(term: str) -> dict[str, Any]:
    """Explain common recruitment, HR, compensation, and contractual terms.

    Args:
        term: The recruitment or HR term to explain (e.g. 'notice period', 'CTC', 'buyout').

    Returns:
        dict: A dictionary containing:
            - 'term': The requested term.
            - 'found': True if the term is in the authoritative glossary, False otherwise.
            - 'explanation': 3-5 line authoritative explanation (if found).
            - 'message': Clarification message if the term was not found.
    """
    clean_term = term.strip().lower().replace("-", " ").replace("_", " ")
    canonical_key = TERM_ALIASES.get(clean_term) or TERM_ALIASES.get(term.strip().lower())

    if canonical_key and canonical_key in GLOSSARY_DEFINITIONS:
        return {
            "term": term,
            "found": True,
            "canonical_term": canonical_key,
            "explanation": GLOSSARY_DEFINITIONS[canonical_key],
        }

    return {
        "term": term,
        "found": False,
        "message": f"Term '{term}' was not found in the recruitment glossary.",
    }
