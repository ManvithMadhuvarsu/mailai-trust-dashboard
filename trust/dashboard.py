"""FastAPI routes for the MailAI trust dashboard and APIs."""

from __future__ import annotations

import os
from datetime import datetime, time, timezone

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from tools.gmail_tool import get_gmail_service
from trust.actions import approve_review_item, reject_review_item, undo_action
from trust.database import get_db
from trust.models import ActionEvent, AuditEvent, ReviewItem
from trust.preferences import export_preferences_yaml, list_preferences, upsert_preference


router = APIRouter()
templates = Jinja2Templates(directory="web/templates")


def _secret() -> str:
    return os.getenv("DASHBOARD_SECRET", "").strip()


def _authorized(request: Request) -> bool:
    secret = _secret()
    if not secret:
        return True
    supplied = (
        request.query_params.get("key")
        or request.headers.get("x-dashboard-secret")
        or request.cookies.get("mailai_dashboard_auth")
    )
    return supplied == secret


def _require_dashboard(request: Request) -> None:
    if not _authorized(request):
        raise HTTPException(status_code=401, detail="Dashboard authorization required")


def _iso(value) -> str | None:
    return value.isoformat() if value else None


def _decode_context(raw: str) -> list:
    import json

    try:
        return json.loads(raw or "[]")
    except Exception:
        return []


def _serialize_audit(audit: AuditEvent) -> dict:
    latest_action = audit.actions[-1] if audit.actions else None
    return {
        "id": audit.id,
        "email_id": audit.email_id,
        "thread_id": audit.thread_id,
        "subject": audit.subject,
        "sender": audit.sender,
        "sender_email": audit.sender_email,
        "job_category": audit.job_category,
        "risk_category": audit.risk_category,
        "confidence": audit.confidence,
        "agent_action": audit.agent_action,
        "policy_action": audit.policy_action,
        "reasoning": audit.reasoning,
        "cited_context": _decode_context(audit.cited_context),
        "requires_review": audit.requires_review,
        "reversible": audit.reversible,
        "status": audit.status,
        "latest_action_id": latest_action.id if latest_action else None,
        "created_at": _iso(audit.created_at),
    }


def _serialize_review(item: ReviewItem) -> dict:
    return {
        "id": item.id,
        "audit_id": item.audit_id,
        "email_id": item.email_id,
        "thread_id": item.thread_id,
        "subject": item.subject,
        "sender": item.sender,
        "sender_email": item.sender_email,
        "reply_to": item.reply_to,
        "risk_category": item.risk_category,
        "job_category": item.job_category,
        "confidence": item.confidence,
        "review_reason": item.review_reason,
        "draft_subject": item.draft_subject,
        "draft_body": item.draft_body,
        "gmail_draft_id": item.gmail_draft_id,
        "status": item.status,
        "created_at": _iso(item.created_at),
        "resolved_at": _iso(item.resolved_at),
    }


def _metrics(db: Session) -> dict:
    queued = db.scalar(select(func.count()).select_from(ReviewItem).where(ReviewItem.status == "queued")) or 0
    audited = db.scalar(select(func.count()).select_from(AuditEvent)) or 0
    actions = db.scalar(select(func.count()).select_from(ActionEvent)) or 0
    reversible = db.scalar(
        select(func.count()).select_from(ActionEvent).where(
            ActionEvent.reversible.is_(True),
            ActionEvent.status == "success",
        )
    ) or 0
    return {
        "queued": queued,
        "audited": audited,
        "actions": actions,
        "reversible": reversible,
    }


@router.get("/dashboard", response_class=HTMLResponse)
def dashboard(request: Request, db: Session = Depends(get_db)):
    secret = _secret()
    key = request.query_params.get("key")
    if secret and key == secret:
        resp = RedirectResponse("/dashboard", status_code=302)
        resp.set_cookie(
            "mailai_dashboard_auth",
            secret,
            httponly=True,
            secure=True,
            samesite="lax",
            max_age=60 * 60 * 12,
        )
        return resp

    if not _authorized(request):
        return templates.TemplateResponse(
            "dashboard_locked.html",
            {"request": request, "secret_configured": bool(secret)},
            status_code=401,
        )

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "metrics": _metrics(db),
            "secret_configured": bool(secret),
            "outbound_mode": os.getenv("OUTBOUND_MODE", "queue_review"),
            "auto_archive": os.getenv("AUTO_ARCHIVE_ENABLED", "false"),
        },
    )


@router.get("/api/audit")
def api_audit(request: Request, limit: int = 50, db: Session = Depends(get_db)):
    _require_dashboard(request)
    limit = max(1, min(limit, 200))
    audits = db.scalars(
        select(AuditEvent)
        .order_by(AuditEvent.created_at.desc(), AuditEvent.id.desc())
        .limit(limit)
    ).all()
    return {"items": [_serialize_audit(item) for item in audits]}


@router.get("/api/review")
def api_review(request: Request, status: str = "queued", db: Session = Depends(get_db)):
    _require_dashboard(request)
    stmt = select(ReviewItem).order_by(ReviewItem.created_at.desc(), ReviewItem.id.desc())
    if status != "all":
        stmt = stmt.where(ReviewItem.status == status)
    items = db.scalars(stmt.limit(100)).all()
    return {"items": [_serialize_review(item) for item in items]}


@router.post("/api/review/{review_id}/approve")
def api_approve_review(review_id: int, request: Request, db: Session = Depends(get_db)):
    _require_dashboard(request)
    try:
        return approve_review_item(db, get_gmail_service(), review_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/review/{review_id}/reject")
async def api_reject_review(review_id: int, request: Request, db: Session = Depends(get_db)):
    _require_dashboard(request)
    payload = await request.json() if request.headers.get("content-type", "").startswith("application/json") else {}
    try:
        return reject_review_item(
            db,
            review_id,
            note=payload.get("note", ""),
            preference=payload.get("preference"),
        )
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.post("/api/actions/{action_id}/undo")
def api_undo(action_id: int, request: Request, db: Session = Depends(get_db)):
    _require_dashboard(request)
    try:
        return undo_action(db, get_gmail_service(), action_id)
    except Exception as e:
        raise HTTPException(status_code=400, detail=str(e)) from e


@router.get("/api/preferences")
def api_preferences(request: Request, db: Session = Depends(get_db)):
    _require_dashboard(request)
    return {"items": list_preferences(db)}


@router.post("/api/preferences")
async def api_create_preference(request: Request, db: Session = Depends(get_db)):
    _require_dashboard(request)
    payload = await request.json()
    pref = upsert_preference(
        db,
        key=payload.get("key", "always_review"),
        value=payload.get("value", True),
        scope_type=payload.get("scope_type", "global"),
        scope_value=payload.get("scope_value", ""),
        source=payload.get("source", "manual"),
    )
    db.commit()
    return {"item": {"id": pref.id, "key": pref.key, "scope_type": pref.scope_type, "scope_value": pref.scope_value}}


@router.get("/api/preferences.yaml", response_class=PlainTextResponse)
def api_preferences_yaml(request: Request, db: Session = Depends(get_db)):
    _require_dashboard(request)
    return PlainTextResponse(export_preferences_yaml(db), media_type="text/yaml")


@router.get("/api/digest/daily")
def api_daily_digest(request: Request, db: Session = Depends(get_db)):
    _require_dashboard(request)
    start = datetime.combine(datetime.now(timezone.utc).date(), time.min, tzinfo=timezone.utc)
    audits = db.scalars(select(AuditEvent).where(AuditEvent.created_at >= start)).all()
    queued = [item for item in audits if item.policy_action == "QUEUE_REVIEW"]
    drafts = [item for item in audits if item.policy_action == "CREATE_DRAFT"]
    labels = [item for item in audits if item.policy_action == "LABEL_ONLY"]
    high_risk = [item for item in audits if item.risk_category in {"FINANCIAL", "LEGAL", "PERSONAL"}]
    return JSONResponse(
        {
            "date": start.date().isoformat(),
            "handled": len(audits),
            "queued_for_review": len(queued),
            "drafts_created": len(drafts),
            "label_only": len(labels),
            "high_risk_blocked": len(high_risk),
            "summary": (
                f"MailAI handled {len(audits)} emails today. "
                f"{len(queued)} are waiting for review. "
                f"{len(high_risk)} high-risk emails were blocked from auto-action."
            ),
        }
    )


@router.get("/api/digest/weekly")
def api_weekly_digest(request: Request, db: Session = Depends(get_db)):
    """
    Module 3 — Weekly preference drift report.
    Returns 7-day stats + detected pattern changes.
    """
    _require_dashboard(request)
    from datetime import timedelta
    import json

    now = datetime.now(timezone.utc)
    week_start = now - timedelta(days=7)
    prev_week_start = week_start - timedelta(days=7)

    this_week = db.scalars(select(AuditEvent).where(AuditEvent.created_at >= week_start)).all()
    prev_week = db.scalars(
        select(AuditEvent).where(
            AuditEvent.created_at >= prev_week_start,
            AuditEvent.created_at < week_start,
        )
    ).all()

    def _by_category(rows):
        out = {}
        for r in rows:
            out[r.job_category] = out.get(r.job_category, 0) + 1
        return out

    this_cats = _by_category(this_week)
    prev_cats = _by_category(prev_week)

    # Detect drift: categories that shifted >25% week-over-week
    drift = []
    all_cats = set(this_cats) | set(prev_cats)
    for cat in all_cats:
        this_n = this_cats.get(cat, 0)
        prev_n = prev_cats.get(cat, 0)
        if prev_n == 0 and this_n > 2:
            drift.append({"category": cat, "change": "new", "this_week": this_n, "prev_week": prev_n})
        elif prev_n > 0:
            pct = (this_n - prev_n) / prev_n
            if abs(pct) >= 0.25:
                drift.append({
                    "category": cat,
                    "change": f"{'+' if pct > 0 else ''}{round(pct * 100)}%",
                    "this_week": this_n,
                    "prev_week": prev_n,
                })

    this_queued = sum(1 for r in this_week if r.requires_review)
    prev_queued = sum(1 for r in prev_week if r.requires_review)
    this_hi_risk = sum(1 for r in this_week if r.risk_category in {"FINANCIAL", "LEGAL", "PERSONAL"})

    # Preference corrections this week
    from trust.models import Preference
    new_prefs = db.scalars(
        select(Preference).where(
            Preference.created_at >= week_start,
            Preference.source == "review_correction",
        )
    ).all()

    return JSONResponse({
        "period": f"{week_start.date().isoformat()} to {now.date().isoformat()}",
        "this_week_total": len(this_week),
        "prev_week_total": len(prev_week),
        "this_week_queued": this_queued,
        "prev_week_queued": prev_queued,
        "high_risk_blocked": this_hi_risk,
        "by_category": this_cats,
        "category_drift": drift,
        "new_learned_preferences": len(new_prefs),
        "learned_preferences_detail": [
            {"key": p.key, "scope_type": p.scope_type, "scope_value": p.scope_value}
            for p in new_prefs
        ],
        "drift_summary": (
            f"This week: {len(this_week)} emails vs {len(prev_week)} last week. "
            f"{len(drift)} category shift(s) detected. "
            f"{len(new_prefs)} new preference rule(s) learned from your corrections."
        ) if drift or new_prefs else (
            f"Stable week: {len(this_week)} emails processed, no significant pattern changes."
        ),
    })


@router.post("/api/actions/undo-last")
async def api_undo_last(request: Request, db: Session = Depends(get_db)):
    """
    Undo last N reversible actions (default 5) within the 24h safety window.
    Body: { "count": 5 }
    """
    _require_dashboard(request)
    from datetime import timedelta

    payload = {}
    if request.headers.get("content-type", "").startswith("application/json"):
        payload = await request.json()

    count = min(int(payload.get("count", 5)), 10)  # cap at 10
    cutoff = datetime.now(timezone.utc) - timedelta(hours=24)

    actions = db.scalars(
        select(ActionEvent)
        .where(
            ActionEvent.reversible.is_(True),
            ActionEvent.status == "success",
            ActionEvent.created_at >= cutoff,
        )
        .order_by(ActionEvent.created_at.desc())
        .limit(count)
    ).all()

    if not actions:
        return JSONResponse({"undone": [], "message": "No reversible actions in the last 24 hours."})

    service = get_gmail_service()
    results = []
    for action in actions:
        try:
            result = undo_action(db, service, action.id)
            results.append({"action_id": action.id, "status": "undone", "type": action.action_type})
        except Exception as e:
            results.append({"action_id": action.id, "status": "failed", "error": str(e)})

    undone_count = sum(1 for r in results if r["status"] == "undone")
    return JSONResponse({
        "undone": results,
        "message": f"Rolled back {undone_count} of {len(results)} actions.",
    })
