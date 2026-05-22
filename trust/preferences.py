"""Preference memory for MailAI corrections and user rules."""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from email.utils import parseaddr
from typing import Any

import yaml
from sqlalchemy import select
from sqlalchemy.orm import Session

from trust.models import Preference


@dataclass(frozen=True)
class PreferenceRule:
    id: int
    key: str
    value: Any
    scope_type: str
    scope_value: str
    source: str


def email_address(raw: str) -> str:
    return parseaddr(raw or "")[1].lower()


def email_domain(raw: str) -> str:
    address = email_address(raw)
    if "@" not in address:
        return ""
    return address.rsplit("@", 1)[1].lower()


def email_sender_domain(email: dict) -> str:
    return email_domain(email.get("sender_email") or email.get("sender") or "")


def _decode_value(value: str) -> Any:
    try:
        return json.loads(value)
    except Exception:
        return value


def _encode_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, sort_keys=True)


def _normalize_scope(scope_type: str, scope_value: str) -> tuple[str, str]:
    normalized_type = (scope_type or "global").strip().lower()
    normalized_value = (scope_value or "").strip().lower()
    if normalized_type == "domain":
        normalized_value = normalized_value.lstrip("@")
    if normalized_type == "sender":
        normalized_value = email_address(normalized_value) or normalized_value
    return normalized_type, normalized_value


def serialize_preference(pref: Preference) -> dict:
    return {
        "id": pref.id,
        "key": pref.key,
        "value": _decode_value(pref.value),
        "scope_type": pref.scope_type,
        "scope_value": pref.scope_value,
        "source": pref.source,
        "created_at": pref.created_at.isoformat() if pref.created_at else None,
        "updated_at": pref.updated_at.isoformat() if pref.updated_at else None,
    }


def list_preferences(db: Session) -> list[dict]:
    prefs = db.scalars(select(Preference).order_by(Preference.updated_at.desc())).all()
    return [serialize_preference(pref) for pref in prefs]


def upsert_preference(
    db: Session,
    *,
    key: str,
    value: Any = True,
    scope_type: str = "global",
    scope_value: str = "",
    source: str = "manual",
) -> Preference:
    scope_type, scope_value = _normalize_scope(scope_type, scope_value)
    pref = db.scalar(
        select(Preference).where(
            Preference.key == key,
            Preference.scope_type == scope_type,
            Preference.scope_value == scope_value,
        )
    )
    if pref is None:
        pref = Preference(
            key=key,
            scope_type=scope_type,
            scope_value=scope_value,
            source=source,
        )
        db.add(pref)

    pref.value = _encode_value(value)
    pref.source = source
    db.flush()
    return pref


def _rule_matches_email(rule: PreferenceRule, email: dict) -> bool:
    if rule.scope_type == "global":
        return True

    sender = email_address(email.get("sender_email") or email.get("sender") or "")
    domain = email_sender_domain(email)
    subject = (email.get("subject") or "").lower()

    if rule.scope_type == "domain":
        expected = rule.scope_value.lstrip("@")
        return domain == expected or domain.endswith(f".{expected}")
    if rule.scope_type == "sender":
        return sender == rule.scope_value
    if rule.scope_type == "subject_contains":
        return rule.scope_value in subject
    if rule.scope_type == "regex":
        merged = " ".join(
            str(email.get(field) or "")
            for field in ("sender", "subject", "snippet", "body")
        ).lower()
        try:
            return bool(re.search(rule.scope_value, merged))
        except re.error:
            return False

    return False


def preferences_for_email(db: Session, email: dict) -> list[PreferenceRule]:
    rows = db.scalars(select(Preference)).all()
    rules = [
        PreferenceRule(
            id=row.id,
            key=row.key,
            value=_decode_value(row.value),
            scope_type=row.scope_type,
            scope_value=row.scope_value,
            source=row.source,
        )
        for row in rows
    ]
    return [rule for rule in rules if _rule_matches_email(rule, email)]


def export_preferences_yaml(db: Session) -> str:
    payload = {"preferences": list_preferences(db)}
    return yaml.safe_dump(payload, sort_keys=False, allow_unicode=False)

