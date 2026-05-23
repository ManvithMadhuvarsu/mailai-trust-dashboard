"""Confidence gates and safety policy for MailAI decisions."""

from __future__ import annotations

import os
from dataclasses import dataclass

from trust.preferences import PreferenceRule, email_sender_domain


DRAFT_ACTIONS = {"DRAFT_FEEDBACK", "DRAFT_CONFIRM", "DRAFT_RESPONSE"}
JOB_LABEL_CATEGORIES = {"REJECTION", "INTERVIEW", "HOLD", "FOLLOW_UP", "APPLIED"}
REVIEW_RISK_CATEGORIES = {"FINANCIAL", "LEGAL", "PERSONAL"}


@dataclass(frozen=True)
class RiskResult:
    category: str
    confidence: float
    reasoning: str
    matched_terms: list[str]


def _env_bool(name: str, default: bool = False) -> bool:
    raw = os.getenv(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name, "").strip()
    if not raw:
        return default
    try:
        return float(raw)
    except ValueError:
        return default


def _merged_text(email: dict) -> str:
    fields = [
        email.get("sender", ""),
        email.get("sender_email", ""),
        email.get("reply_to", ""),
        email.get("subject", ""),
        email.get("snippet", ""),
        email.get("body", ""),
    ]
    return " ".join(str(value).lower() for value in fields if value)


def email_has_noreply_details(email: dict) -> bool:
    patterns = ["noreply", "no-reply", "donotreply", "do-not-reply"]
    return any(pattern in _merged_text(email) for pattern in patterns)


def _matches(text: str, terms: list[str]) -> list[str]:
    return [term for term in terms if term in text]


def evaluate_risk(email: dict, job_category: str, agent_action: str) -> RiskResult:
    text = _merged_text(email)
    domain = email_sender_domain(email)

    financial_terms = [
        "invoice", "payment", "bank", "upi", "tax", "refund", "receipt",
        "credit card", "debit card", "payroll", "salary slip", "wire transfer",
    ]
    legal_terms = [
        "contract", "agreement", "nda", "legal notice", "compliance",
        "terms of service", "policy update", "court", "attorney",
    ]
    personal_terms = [
        "personal", "private", "family", "medical", "health", "doctor",
        "password", "otp", "one-time password", "verification code",
    ]
    vendor_terms = [
        "subscription", "renewal", "workspace", "vendor", "billing",
        "account alert", "security alert", "service update",
    ]
    junk_terms = [
        "unsubscribe", "limited time", "sale", "discount", "promotion",
        "newsletter", "webinar", "digest", "sponsored",
    ]
    fyi_terms = [
        "for your information", "fyi", "update", "notification",
        "application received", "thank you for applying", "under review",
    ]

    for category, confidence, terms, reason in [
        ("FINANCIAL", 0.94, financial_terms, "Financial language requires manual review."),
        ("LEGAL", 0.94, legal_terms, "Legal or compliance language requires manual review."),
        ("PERSONAL", 0.90, personal_terms, "Personal or sensitive language requires manual review."),
        ("VENDOR", 0.84, vendor_terms, "Vendor/account language should be handled cautiously."),
        ("JUNK", 0.88, junk_terms, "Promotional/newsletter signals detected."),
        ("FYI", 0.82, fyi_terms, "Informational status update detected."),
    ]:
        matched = _matches(text, terms)
        if matched:
            return RiskResult(category, confidence, reason, matched)

    free_domains = {"gmail.com", "outlook.com", "hotmail.com", "icloud.com", "yahoo.com"}
    if domain in free_domains and job_category not in JOB_LABEL_CATEGORIES:
        return RiskResult("PERSONAL", 0.72, "Personal email domain without a clear job category.", [domain])

    if agent_action in DRAFT_ACTIONS or job_category in {"INTERVIEW", "FOLLOW_UP", "REJECTION"}:
        return RiskResult("ACTION_REQUIRED", 0.84, "Job workflow needs a response or review.", [job_category])

    if job_category in {"APPLIED", "HOLD"}:
        return RiskResult("FYI", 0.78, "Job workflow status update; label-only is usually safe.", [job_category])

    if job_category == "IRRELEVANT":
        return RiskResult("JUNK", 0.70, "No job workflow signal detected.", [job_category])

    return RiskResult("FYI", 0.65, "No high-risk signal detected; defaulting to FYI.", [])


def _preference_truthy(rule: PreferenceRule) -> bool:
    if isinstance(rule.value, bool):
        return rule.value
    if isinstance(rule.value, str):
        return rule.value.strip().lower() not in {"", "false", "0", "no", "off"}
    return bool(rule.value)


def build_trust_decision(
    email: dict,
    result: dict,
    preferences: list[PreferenceRule] | None = None,
) -> dict:
    preferences = preferences or []
    job_category = result.get("category") or result.get("job_category") or "IRRELEVANT"
    agent_action = result.get("action") or "SKIP"
    risk = evaluate_risk(email, job_category, agent_action)
    no_reply = email_has_noreply_details(email)

    policy_action = "SKIP"
    requires_review = False
    allow_label = job_category in JOB_LABEL_CATEGORIES
    review_reason = ""
    reasons = [risk.reasoning]
    matched_terms = list(risk.matched_terms)

    forced_risk = None
    for rule in preferences:
        if rule.key == "label_as_fyi" and _preference_truthy(rule):
            forced_risk = "FYI"
            reasons.append(f"Preference #{rule.id}: classify matching mail as FYI.")
        if rule.key == "never_draft" and _preference_truthy(rule) and agent_action in DRAFT_ACTIONS:
            no_reply = True
            reasons.append(f"Preference #{rule.id}: never draft replies for this scope.")

    if forced_risk:
        risk = RiskResult(forced_risk, max(risk.confidence, 0.90), "User preference override.", matched_terms)

    always_review = any(
        rule.key == "always_review" and _preference_truthy(rule)
        for rule in preferences
    )

    action_threshold = _env_float("MIN_ACTION_CONFIDENCE", 0.74)
    archive_threshold = _env_float("MIN_AUTO_ARCHIVE_CONFIDENCE", 0.92)
    auto_archive = _env_bool("AUTO_ARCHIVE_ENABLED", False)
    outbound_mode = os.getenv("OUTBOUND_MODE", "queue_review").strip().lower()
    drafts_disabled = _env_bool("DISABLE_DRAFTS", False)

    if always_review:
        policy_action = "QUEUE_REVIEW"
        requires_review = True
        allow_label = False
        review_reason = "User preference requires manual review."
    elif no_reply and agent_action in DRAFT_ACTIONS:
        policy_action = "LABEL_ONLY" if allow_label else "SKIP"
        requires_review = False
        review_reason = "No-reply sender/content blocked reply drafting."
        reasons.append("No-reply signal found in sender, address, subject, snippet, or body.")
    elif risk.category in REVIEW_RISK_CATEGORIES:
        policy_action = "QUEUE_REVIEW"
        requires_review = True
        allow_label = False
        review_reason = f"{risk.category.title()} mail is never auto-acted on."
    elif risk.category == "ACTION_REQUIRED" and risk.confidence < action_threshold:
        policy_action = "QUEUE_REVIEW"
        requires_review = True
        allow_label = False
        review_reason = "Action-required confidence is below the safety threshold."
    elif agent_action in DRAFT_ACTIONS:
        if drafts_disabled:
            policy_action = "LABEL_ONLY" if allow_label else "SKIP"
            review_reason = "Draft generation disabled for this run."
        elif outbound_mode == "gmail_draft":
            policy_action = "CREATE_DRAFT"
            review_reason = "Configured to create Gmail drafts, never send."
        else:
            policy_action = "QUEUE_REVIEW"
            requires_review = True
            review_reason = "Approval gate enabled; generated reply is queued for review."
    elif risk.category in {"FYI", "JUNK"} and auto_archive and risk.confidence >= archive_threshold:
        policy_action = "ARCHIVE"
        review_reason = "High-confidence FYI/JUNK auto-archive is enabled."
    elif agent_action == "LABEL_ONLY":
        policy_action = "LABEL_ONLY" if allow_label else "SKIP"
        review_reason = "Label-only job workflow action."
    else:
        policy_action = "SKIP"
        allow_label = False
        review_reason = "No safe action required."

    cited_context = [
        {"source": "subject", "text": (email.get("subject") or "")[:180]},
        {"source": "sender", "text": (email.get("sender") or "")[:180]},
        {"source": "snippet", "text": (email.get("snippet") or email.get("body") or "")[:260]},
    ]

    return {
        "email_id": email.get("id", ""),
        "thread_id": email.get("thread_id", ""),
        "job_category": job_category,
        "risk_category": risk.category,
        "confidence": round(float(risk.confidence), 3),
        "action": agent_action,
        "policy_action": policy_action,
        "reasoning": " ".join(reason for reason in reasons if reason),
        "cited_context": cited_context,
        "matched_terms": matched_terms,
        "requires_review": requires_review,
        "reversible": policy_action in {"LABEL_ONLY", "CREATE_DRAFT", "ARCHIVE", "QUEUE_REVIEW"},
        "allow_label": allow_label,
        "review_reason": review_reason,
        "no_reply_detected": no_reply,
        "outbound_mode": outbound_mode,
    }



# ── Module 4: Tone Classifier ─────────────────────────────────────────────────

_HIGH_FORMALITY = {
    "pursuant", "hereinafter", "notwithstanding", "aforementioned",
    "heretofore", "whilst", "therein", "wherein", "henceforth",
    "herewith", "forthwith", "duly", "aforestated",
}

_LOW_FORMALITY = {
    "hey", "yo", "lol", "btw", "gonna", "wanna", "kinda", "sorta",
    "damn", "crap", "wtf", "omg", "nope", "yep", "cool", "ok",
}

_AGGRESSIVE_SIGNALS = [
    "this is unacceptable", "you failed", "i demand", "legal action",
    "this is ridiculous", "absolutely terrible", "worst experience",
]


def score_draft_tone(draft_body: str) -> dict:
    """
    Score the formality and tone of a draft reply.
    Returns a dict with score (0=informal, 1=formal), delta from baseline,
    flagged bool, and detected signals.
    """
    if not draft_body:
        return {"score": 0.5, "delta": 0.0, "flagged": False, "signals": []}

    words = draft_body.lower().split()
    total = max(len(words), 1)
    clean = [w.strip(".,!?;:\"'") for w in words]

    high_count = sum(1 for w in clean if w in _HIGH_FORMALITY)
    low_count = sum(1 for w in clean if w in _LOW_FORMALITY)

    score = 0.5 + (high_count * 0.05) - (low_count * 0.08)
    score = max(0.0, min(1.0, round(score, 3)))

    # Baseline for job-search emails is ~0.65 (professional)
    baseline = float(os.getenv("TONE_BASELINE_SCORE", "0.65"))
    delta = round(abs(score - baseline), 3)
    threshold = float(os.getenv("TONE_FLAG_THRESHOLD", "0.30"))

    signals = []
    if high_count:
        signals.append(f"over-formal ({high_count} legalese word(s))")
    if low_count:
        signals.append(f"too casual ({low_count} informal word(s))")

    body_lower = draft_body.lower()
    for phrase in _AGGRESSIVE_SIGNALS:
        if phrase in body_lower:
            signals.append(f"aggressive phrase: '{phrase}'")

    flagged = delta > threshold or any("aggressive" in s for s in signals)

    return {
        "score": score,
        "baseline": baseline,
        "delta": delta,
        "flagged": flagged,
        "signals": signals,
    }


def apply_tone_check(decision: dict, draft_body: str) -> dict:
    """
    Run tone check on a draft and inject result into the decision dict.
    If flagged, escalate policy_action to QUEUE_REVIEW.
    """
    if not draft_body:
        return decision

    tone = score_draft_tone(draft_body)
    decision["tone_check"] = tone

    if tone["flagged"] and decision.get("policy_action") == "CREATE_DRAFT":
        decision["policy_action"] = "QUEUE_REVIEW"
        decision["requires_review"] = True
        decision["review_reason"] = (
            decision.get("review_reason", "") +
            f" Tone flagged: {'; '.join(tone['signals'])}."
        ).strip()

    return decision
