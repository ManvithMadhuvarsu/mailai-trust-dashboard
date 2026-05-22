from trust.policy import build_trust_decision
from trust.preferences import PreferenceRule


def email(**overrides):
    base = {
        "id": "msg-1",
        "thread_id": "thread-1",
        "subject": "Interview invitation",
        "sender": "Recruiter <recruiter@example.com>",
        "sender_email": "recruiter@example.com",
        "reply_to": "recruiter@example.com",
        "snippet": "Please share your availability.",
        "body": "Please share your availability for the next round.",
    }
    base.update(overrides)
    return base


def result(**overrides):
    base = {
        "category": "INTERVIEW",
        "action": "DRAFT_CONFIRM",
        "draft_subject": "Re: Interview invitation",
        "draft_body": "Thank you for the invitation.",
    }
    base.update(overrides)
    return base


def test_no_reply_blocks_reply_drafts(monkeypatch):
    monkeypatch.setenv("OUTBOUND_MODE", "gmail_draft")
    decision = build_trust_decision(
        email(sender_email="donotreply@example.com", body="DoNotReply mailbox"),
        result(),
    )

    assert decision["policy_action"] == "LABEL_ONLY"
    assert decision["no_reply_detected"] is True
    assert decision["requires_review"] is False


def test_financial_mail_is_queued_and_not_labeled(monkeypatch):
    monkeypatch.setenv("OUTBOUND_MODE", "gmail_draft")
    decision = build_trust_decision(
        email(subject="Invoice payment needed", body="Please review this invoice payment."),
        result(category="FOLLOW_UP", action="DRAFT_RESPONSE"),
    )

    assert decision["risk_category"] == "FINANCIAL"
    assert decision["policy_action"] == "QUEUE_REVIEW"
    assert decision["requires_review"] is True
    assert decision["allow_label"] is False


def test_default_outbound_mode_queues_generated_replies(monkeypatch):
    monkeypatch.delenv("OUTBOUND_MODE", raising=False)
    decision = build_trust_decision(email(), result())

    assert decision["policy_action"] == "QUEUE_REVIEW"
    assert decision["requires_review"] is True
    assert decision["allow_label"] is True


def test_never_draft_preference_downgrades_to_label_only(monkeypatch):
    monkeypatch.setenv("OUTBOUND_MODE", "gmail_draft")
    preference = PreferenceRule(
        id=7,
        key="never_draft",
        value=True,
        scope_type="domain",
        scope_value="example.com",
        source="test",
    )
    decision = build_trust_decision(email(), result(), [preference])

    assert decision["policy_action"] == "LABEL_ONLY"
    assert "never draft" in decision["reasoning"].lower()

