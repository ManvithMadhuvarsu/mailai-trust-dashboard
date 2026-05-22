"""
backfill.py

Safely label emails for the last N days that are not yet labeled with MailAI labels.

Design goals:
- Chunk by date window to avoid huge pulls
- Label-only by default (no drafts) to minimize LLM load
- Ollama-only option to avoid Groq usage
- Resumable and crash-safe via small window processing + existing processed tracking
"""

import os
import sys
import time
import logging
from datetime import datetime, timedelta
from pathlib import Path

from dotenv import load_dotenv

load_dotenv()

# Must run before importing classifier_agent: load_dotenv() may set USE_OLLAMA=false;
# setdefault() would not override, and Groq would run (see backfill.err.log 429s).
def _force_ollama_for_backfill() -> None:
    if os.getenv("BACKFILL_ALLOW_GROQ", "").strip().lower() in {"1", "true", "yes"}:
        return
    os.environ["DISABLE_DRAFTS"] = "true"
    os.environ["USE_OLLAMA"] = "true"
    os.environ["REQUIRE_OLLAMA"] = "true"


_force_ollama_for_backfill()

from tools.gmail_tool import (
    get_gmail_service,
    fetch_emails_by_query,
    get_or_create_label,
)
from agents.classifier_agent import process_email
from trust.actions import execute_trust_decision
from trust.database import SessionLocal, init_db
from trust.policy import build_trust_decision
from trust.preferences import preferences_for_email


logger = logging.getLogger("backfill")


LABEL_MAP = {
    "REJECTION": os.getenv("LABEL_REJECTION", "Job/Rejection"),
    "INTERVIEW": os.getenv("LABEL_INTERVIEW", "Job/Interview"),
    "HOLD":      os.getenv("LABEL_HOLD",      "Job/On-Hold"),
    "FOLLOW_UP": os.getenv("LABEL_FOLLOWUP",  "Job/Follow-Up"),
    "APPLIED":   os.getenv("LABEL_APPLIED",   "Job/Applied"),
}


def _gmail_date(d: datetime) -> str:
    return d.strftime("%Y/%m/%d")


def _build_unlabeled_query(after: datetime, before: datetime) -> str:
    # Search everywhere, not only inbox.
    q = f"after:{_gmail_date(after)} before:{_gmail_date(before)} in:anywhere"
    # Exclude already labeled by our labels.
    for name in LABEL_MAP.values():
        # Gmail search supports quotes around label names containing slashes.
        q += f' -label:"{name}"'
    return q


def backfill():
    days_back = int(os.getenv("BACKFILL_DAYS", "").strip() or 365)
    window_days = int(os.getenv("BACKFILL_WINDOW_DAYS", "").strip() or 7)
    max_per_window = int(os.getenv("BACKFILL_MAX_PER_WINDOW", "").strip() or 200)
    sleep_seconds = float(os.getenv("BACKFILL_SLEEP_SECONDS", "").strip() or 1.5)
    start_date_raw = os.getenv("BACKFILL_START_DATE", "").strip()
    end_date_raw = os.getenv("BACKFILL_END_DATE", "").strip()

    range_desc = (
        f"{start_date_raw or f'{days_back}d back'} -> {end_date_raw or 'today'}"
        if (start_date_raw or end_date_raw)
        else f"last {days_back} days"
    )
    logger.info(
        f"Backfill starting: range={range_desc}, window_days={window_days}, max_per_window={max_per_window}"
    )
    print(f"Backfill range: {range_desc}; window {window_days} days; up to {max_per_window} fetch per pass (repeat until empty)")
    print("LLM: USE_OLLAMA=true, REQUIRE_OLLAMA=true, DISABLE_DRAFTS=true (labels only)")

    init_db()
    service = get_gmail_service()
    db = SessionLocal()

    # Ensure labels exist once
    label_ids = {k: get_or_create_label(service, v) for k, v in LABEL_MAP.items()}

    now = datetime.now()
    if start_date_raw:
        start = datetime.strptime(start_date_raw, "%Y-%m-%d")
    else:
        start = now - timedelta(days=days_back)

    if end_date_raw:
        end = datetime.strptime(end_date_raw, "%Y-%m-%d") + timedelta(days=1)
    else:
        end = now

    cursor = start
    total = 0
    try:
        while cursor < end:
            window_end = min(cursor + timedelta(days=window_days), end)
            print(f"\nWindow: {cursor.date()} -> {window_end.date()}")

            # Repeat fetches until this date slice has no remaining unlabeled matches
            # (avoids skipping when count exceeds max_per_window in one week).
            pass_num = 0
            while True:
                query = _build_unlabeled_query(cursor, window_end)
                emails = fetch_emails_by_query(service, query=query, max_total=max_per_window)
                pass_num += 1
                print(f"  Pass {pass_num}: found {len(emails)} candidates")
                if not emails:
                    break

                for email in emails:
                    result = process_email(email)
                    preferences = preferences_for_email(db, email)
                    decision = build_trust_decision(email, result, preferences)
                    result.update(decision)
                    execute_trust_decision(
                        db=db,
                        service=service,
                        email=email,
                        result=result,
                        decision=decision,
                        label_ids=label_ids,
                    )
                    total += 1
                    time.sleep(sleep_seconds)

                if len(emails) < max_per_window:
                    break

            cursor = window_end
    finally:
        db.close()

    print(f"\nBackfill complete. Labeled/processed {total} messages.")


if __name__ == "__main__":
    try:
        backfill()
    except KeyboardInterrupt:
        print("\nBackfill interrupted.")
        sys.exit(1)
