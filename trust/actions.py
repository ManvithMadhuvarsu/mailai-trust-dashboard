"""Audited Gmail action executor for the MailAI trust layer."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timedelta, timezone
from typing import Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from tools.gmail_tool import (
    apply_label,
    archive_message,
    delete_draft,
    remove_label,
    restore_message_to_inbox,
    save_draft,
)
from trust.models import ActionEvent, AuditEvent, ReviewItem, utcnow
from trust.preferences import upsert_preference


logger = logging.getLogger(__name__)


def _json(data) -> str:
    return json.dumps(data or {}, ensure_ascii=True, sort_keys=True, default=str)


def _loads(raw: str) -> dict:
    try:
        return json.loads(raw or "{}")
    except Exception:
        return {}


def _utc_aware(value: datetime | None) -> datetime:
    if value is None:
        return utcnow()
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _email_snapshot(email: dict) -> dict:
    return {
        "id": email.get("id", ""),
        "thread_id": email.get("thread_id", ""),
        "subject": email.get("subject", ""),
        "sender": email.get("sender", ""),
        "sender_email": email.get("sender_email", ""),
        "reply_to": email.get("reply_to", ""),
        "date": email.get("date", ""),
        "label_ids": email.get("label_ids", []),
        "snippet": email.get("snippet", ""),
    }


def record_audit_event(db: Session, email: dict, result: dict, decision: dict) -> AuditEvent:
    audit = AuditEvent(
        email_id=email.get("id", ""),
        thread_id=email.get("thread_id", ""),
        subject=email.get("subject", ""),
        sender=email.get("sender", ""),
        sender_email=email.get("sender_email", ""),
        reply_to=email.get("reply_to", ""),
        job_category=decision.get("job_category", result.get("category", "IRRELEVANT")),
        risk_category=decision.get("risk_category", "FYI"),
        confidence=float(decision.get("confidence", 0.0)),
        agent_action=decision.get("action", result.get("action", "SKIP")),
        policy_action=decision.get("policy_action", "SKIP"),
        reasoning=decision.get("reasoning", ""),
        cited_context=_json(decision.get("cited_context", [])),
        email_payload=_json(_email_snapshot(email)),
        decision_payload=_json(decision),
        requires_review=bool(decision.get("requires_review", False)),
        reversible=bool(decision.get("reversible", False)),
        status="recorded",
    )
    db.add(audit)
    db.flush()
    return audit


def record_action(
    db: Session,
    *,
    audit: AuditEvent | None,
    email_id: str,
    action_type: str,
    status: str = "success",
    reversible: bool = False,
    rollback_type: str = "",
    rollback_payload: dict | None = None,
    error: str = "",
) -> ActionEvent:
    action = ActionEvent(
        audit_id=audit.id if audit else None,
        email_id=email_id,
        action_type=action_type,
        status=status,
        reversible=reversible,
        rollback_type=rollback_type,
        rollback_payload=_json(rollback_payload or {}),
        error=error,
    )
    db.add(action)
    db.flush()
    return action


def queue_review_item(db: Session, audit: AuditEvent, email: dict, result: dict, decision: dict) -> ReviewItem:
    existing = db.scalar(
        select(ReviewItem).where(
            ReviewItem.email_id == email.get("id", ""),
            ReviewItem.status == "queued",
        )
    )
    item = existing or ReviewItem(
        audit_id=audit.id,
        email_id=email.get("id", ""),
        thread_id=email.get("thread_id", ""),
    )
    item.audit_id = audit.id
    item.subject = email.get("subject", "")
    item.sender = email.get("sender", "")
    item.sender_email = email.get("sender_email", "")
    item.reply_to = email.get("reply_to") or email.get("sender", "")
    item.risk_category = decision.get("risk_category", "FYI")
    item.job_category = decision.get("job_category", result.get("category", "IRRELEVANT"))
    item.confidence = float(decision.get("confidence", 0.0))
    item.review_reason = decision.get("review_reason", "")
    item.draft_subject = result.get("draft_subject", "")
    item.draft_body = result.get("draft_body", "")
    item.status = "queued"
    item.updated_at = utcnow()
    if existing is None:
        db.add(item)
    db.flush()
    return item


def execute_trust_decision(
    *,
    db: Session,
    service,
    email: dict,
    result: dict,
    decision: dict,
    label_ids: dict[str, str | None],
    thread_has_draft: Callable[[object, str], bool] | None = None,
) -> dict:
    audit = record_audit_event(db, email, result, decision)
    category = decision.get("job_category", result.get("category", "IRRELEVANT"))
    policy_action = decision.get("policy_action", "SKIP")
    email_id = email.get("id", "")
    outcome = {
        "audit_id": audit.id,
        "draft_saved": False,
        "queued": False,
        "label_applied": False,
        "archived": False,
        "skipped": False,
    }

    label_id = label_ids.get(category)
    if decision.get("allow_label") and label_id:
        ok = apply_label(service, email_id, label_id)
        outcome["label_applied"] = ok
        record_action(
            db,
            audit=audit,
            email_id=email_id,
            action_type="APPLY_LABEL",
            status="success" if ok else "failed",
            reversible=ok,
            rollback_type="remove_label" if ok else "",
            rollback_payload={"message_id": email_id, "label_id": label_id, "category": category},
            error="" if ok else "Gmail label application failed",
        )

    if policy_action == "QUEUE_REVIEW":
        review = queue_review_item(db, audit, email, result, decision)
        outcome["queued"] = True
        record_action(
            db,
            audit=audit,
            email_id=email_id,
            action_type="QUEUE_REVIEW",
            reversible=True,
            rollback_type="close_review",
            rollback_payload={"review_id": review.id},
        )
    elif policy_action == "CREATE_DRAFT":
        thread_id = email.get("thread_id", "")
        if thread_has_draft and thread_has_draft(service, thread_id):
            record_action(
                db,
                audit=audit,
                email_id=email_id,
                action_type="CREATE_DRAFT",
                status="skipped",
                error="Draft already exists for this thread",
            )
        else:
            draft_id = save_draft(
                service=service,
                to=email.get("reply_to") or email.get("sender", ""),
                subject=result.get("draft_subject", f"Re: {email.get('subject', '')}"),
                body=result.get("draft_body", ""),
                thread_id=thread_id,
            )
            outcome["draft_saved"] = bool(draft_id)
            record_action(
                db,
                audit=audit,
                email_id=email_id,
                action_type="CREATE_DRAFT",
                status="success" if draft_id else "failed",
                reversible=bool(draft_id),
                rollback_type="delete_draft" if draft_id else "",
                rollback_payload={"draft_id": draft_id},
                error="" if draft_id else "Gmail draft creation failed",
            )
    elif policy_action == "ARCHIVE":
        ok = archive_message(service, email_id)
        outcome["archived"] = ok
        record_action(
            db,
            audit=audit,
            email_id=email_id,
            action_type="ARCHIVE",
            status="success" if ok else "failed",
            reversible=ok,
            rollback_type="restore_inbox" if ok else "",
            rollback_payload={"message_id": email_id},
            error="" if ok else "Gmail archive failed",
        )
    else:
        outcome["skipped"] = True
        record_action(
            db,
            audit=audit,
            email_id=email_id,
            action_type=policy_action or "SKIP",
            status="success",
        )

    audit.status = "complete"
    db.commit()
    return outcome


def approve_review_item(db: Session, service, review_id: int) -> dict:
    item = db.get(ReviewItem, review_id)
    if item is None:
        raise ValueError("Review item not found")
    if item.status != "queued":
        raise ValueError(f"Review item is not queued: {item.status}")

    draft_id = ""
    if item.draft_body:
        draft_id = save_draft(
            service=service,
            to=item.reply_to or item.sender,
            subject=item.draft_subject or f"Re: {item.subject}",
            body=item.draft_body,
            thread_id=item.thread_id,
        ) or ""
        if not draft_id:
            raise ValueError("Gmail draft creation failed")

    item.status = "approved"
    item.gmail_draft_id = draft_id
    item.resolved_at = utcnow()
    item.updated_at = utcnow()

    record_action(
        db,
        audit=item.audit,
        email_id=item.email_id,
        action_type="APPROVE_REVIEW",
        reversible=True,
        rollback_type="delete_draft" if draft_id else "reopen_review",
        rollback_payload={"draft_id": draft_id, "review_id": item.id},
    )
    db.commit()
    return {"review_id": item.id, "status": item.status, "gmail_draft_id": draft_id}


def reject_review_item(
    db: Session,
    review_id: int,
    *,
    note: str = "",
    preference: dict | None = None,
) -> dict:
    item = db.get(ReviewItem, review_id)
    if item is None:
        raise ValueError("Review item not found")
    if item.status != "queued":
        raise ValueError(f"Review item is not queued: {item.status}")

    item.status = "rejected"
    item.resolution_note = note
    item.resolved_at = utcnow()
    item.updated_at = utcnow()

    if preference:
        upsert_preference(
            db,
            key=preference.get("key", "always_review"),
            value=preference.get("value", True),
            scope_type=preference.get("scope_type", "domain"),
            scope_value=preference.get("scope_value", item.sender_email),
            source="review_correction",
        )

    record_action(
        db,
        audit=item.audit,
        email_id=item.email_id,
        action_type="REJECT_REVIEW",
        reversible=True,
        rollback_type="reopen_review",
        rollback_payload={"review_id": item.id},
    )
    db.commit()
    return {"review_id": item.id, "status": item.status}


def undo_action(db: Session, service, action_id: int) -> dict:
    action = db.get(ActionEvent, action_id)
    if action is None:
        raise ValueError("Action not found")
    if not action.reversible:
        raise ValueError("Action is not reversible")
    if action.status == "undone":
        raise ValueError("Action has already been undone")

    age = utcnow() - _utc_aware(action.created_at)
    if age > timedelta(hours=24):
        raise ValueError("Undo window expired; only actions from the last 24 hours can be undone")

    payload = _loads(action.rollback_payload)
    rollback_type = action.rollback_type
    ok = True

    if rollback_type == "remove_label":
        ok = remove_label(service, payload.get("message_id", ""), payload.get("label_id", ""))
    elif rollback_type == "delete_draft":
        draft_id = payload.get("draft_id", "")
        ok = delete_draft(service, draft_id) if draft_id else True
        review_id = payload.get("review_id")
        if review_id:
            item = db.get(ReviewItem, int(review_id))
            if item:
                item.status = "queued"
                item.gmail_draft_id = ""
                item.resolved_at = None
    elif rollback_type == "restore_inbox":
        ok = restore_message_to_inbox(service, payload.get("message_id", ""))
    elif rollback_type == "close_review":
        item = db.get(ReviewItem, int(payload.get("review_id", 0)))
        if item:
            item.status = "undone"
            item.resolved_at = utcnow()
            item.updated_at = utcnow()
    elif rollback_type == "reopen_review":
        item = db.get(ReviewItem, int(payload.get("review_id", 0)))
        if item:
            item.status = "queued"
            item.resolved_at = None
            item.updated_at = utcnow()
    else:
        raise ValueError("Unknown rollback type")

    if not ok:
        action.undo_error = "Gmail rollback failed"
        db.commit()
        raise ValueError(action.undo_error)

    action.status = "undone"
    action.undone_at = utcnow()
    db.commit()
    return {"action_id": action.id, "status": action.status, "rollback_type": rollback_type}
