"""SQLAlchemy models for MailAI trust state."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import Boolean, DateTime, Float, ForeignKey, Integer, String, Text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from trust.database import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class AuditEvent(Base):
    __tablename__ = "audit_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    email_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    sender: Mapped[str] = mapped_column(Text, default="")
    sender_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    reply_to: Mapped[str] = mapped_column(Text, default="")
    job_category: Mapped[str] = mapped_column(String(64), default="IRRELEVANT", index=True)
    risk_category: Mapped[str] = mapped_column(String(64), default="FYI", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    agent_action: Mapped[str] = mapped_column(String(64), default="SKIP", index=True)
    policy_action: Mapped[str] = mapped_column(String(64), default="SKIP", index=True)
    reasoning: Mapped[str] = mapped_column(Text, default="")
    cited_context: Mapped[str] = mapped_column(Text, default="[]")
    email_payload: Mapped[str] = mapped_column(Text, default="{}")
    decision_payload: Mapped[str] = mapped_column(Text, default="{}")
    requires_review: Mapped[bool] = mapped_column(Boolean, default=False, index=True)
    reversible: Mapped[bool] = mapped_column(Boolean, default=False)
    status: Mapped[str] = mapped_column(String(32), default="recorded", index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

    actions: Mapped[list["ActionEvent"]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
    )
    review_items: Mapped[list["ReviewItem"]] = relationship(
        back_populates="audit",
        cascade="all, delete-orphan",
    )


class ActionEvent(Base):
    __tablename__ = "action_events"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    audit_id: Mapped[int | None] = mapped_column(ForeignKey("audit_events.id"), nullable=True, index=True)
    email_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    action_type: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), default="success", index=True)
    reversible: Mapped[bool] = mapped_column(Boolean, default=False)
    rollback_type: Mapped[str] = mapped_column(String(64), default="")
    rollback_payload: Mapped[str] = mapped_column(Text, default="{}")
    error: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    undone_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    undo_error: Mapped[str] = mapped_column(Text, default="")

    audit: Mapped[AuditEvent | None] = relationship(back_populates="actions")


class ReviewItem(Base):
    __tablename__ = "review_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    audit_id: Mapped[int] = mapped_column(ForeignKey("audit_events.id"), index=True)
    email_id: Mapped[str] = mapped_column(String(128), index=True)
    thread_id: Mapped[str] = mapped_column(String(128), default="", index=True)
    subject: Mapped[str] = mapped_column(Text, default="")
    sender: Mapped[str] = mapped_column(Text, default="")
    sender_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    reply_to: Mapped[str] = mapped_column(Text, default="")
    risk_category: Mapped[str] = mapped_column(String(64), default="FYI", index=True)
    job_category: Mapped[str] = mapped_column(String(64), default="IRRELEVANT", index=True)
    confidence: Mapped[float] = mapped_column(Float, default=0.0)
    review_reason: Mapped[str] = mapped_column(Text, default="")
    draft_subject: Mapped[str] = mapped_column(Text, default="")
    draft_body: Mapped[str] = mapped_column(Text, default="")
    gmail_draft_id: Mapped[str] = mapped_column(String(128), default="")
    status: Mapped[str] = mapped_column(String(32), default="queued", index=True)
    resolution_note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)

    audit: Mapped[AuditEvent] = relationship(back_populates="review_items")


class Preference(Base):
    __tablename__ = "preferences"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, index=True)
    key: Mapped[str] = mapped_column(String(96), index=True)
    value: Mapped[str] = mapped_column(Text, default="true")
    scope_type: Mapped[str] = mapped_column(String(32), default="global", index=True)
    scope_value: Mapped[str] = mapped_column(String(320), default="", index=True)
    source: Mapped[str] = mapped_column(String(64), default="manual")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, index=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow, onupdate=utcnow)

