from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from trust.actions import execute_trust_decision
from trust.database import Base
from trust.models import ActionEvent, ReviewItem


def make_session():
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    Session = sessionmaker(bind=engine, expire_on_commit=False, future=True)
    return Session()


def test_execute_queues_review_and_records_label(monkeypatch):
    db = make_session()
    calls = []
    monkeypatch.setattr("trust.actions.apply_label", lambda service, message_id, label_id: calls.append((message_id, label_id)) or True)

    email = {
        "id": "msg-1",
        "thread_id": "thread-1",
        "subject": "Interview",
        "sender": "Recruiter <recruiter@example.com>",
        "sender_email": "recruiter@example.com",
        "reply_to": "recruiter@example.com",
    }
    result = {
        "category": "INTERVIEW",
        "action": "DRAFT_CONFIRM",
        "draft_subject": "Re: Interview",
        "draft_body": "Thanks for the invitation.",
    }
    decision = {
        "job_category": "INTERVIEW",
        "risk_category": "ACTION_REQUIRED",
        "confidence": 0.84,
        "action": "DRAFT_CONFIRM",
        "policy_action": "QUEUE_REVIEW",
        "reasoning": "Approval gate.",
        "cited_context": [],
        "requires_review": True,
        "reversible": True,
        "allow_label": True,
        "review_reason": "Approval gate enabled.",
    }

    outcome = execute_trust_decision(
        db=db,
        service=object(),
        email=email,
        result=result,
        decision=decision,
        label_ids={"INTERVIEW": "Label_Interview"},
    )

    assert outcome["queued"] is True
    assert outcome["label_applied"] is True
    assert calls == [("msg-1", "Label_Interview")]
    assert db.scalar(select(ReviewItem).where(ReviewItem.email_id == "msg-1")) is not None
    assert len(db.scalars(select(ActionEvent)).all()) == 2

