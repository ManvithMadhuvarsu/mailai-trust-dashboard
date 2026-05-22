"""
main.py
MailAI orchestrator.

Run once:
  python main.py

Run continuously:
  python daemon.py
"""

import json
import logging
import os
import sys
import time
from datetime import datetime
from pathlib import Path

from colorama import Fore, Style, init
from dotenv import load_dotenv

load_dotenv()
init(autoreset=True)
sys.path.insert(0, str(Path(__file__).parent))

LOG_DIR = Path("data")
LOG_DIR.mkdir(exist_ok=True)
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s - %(message)s",
    handlers=[
        logging.FileHandler(LOG_DIR / "agent.log", encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
logging.getLogger("googleapiclient.discovery").setLevel(logging.WARNING)
logging.getLogger("google.auth").setLevel(logging.WARNING)
logging.getLogger("urllib3").setLevel(logging.WARNING)
logging.getLogger("httpx").setLevel(logging.WARNING)

logger = logging.getLogger(__name__)

from agents.classifier_agent import process_email
from tools.gmail_tool import fetch_recent_emails, get_gmail_service, get_or_create_label
from trust.actions import execute_trust_decision
from trust.database import SessionLocal, init_db
from trust.policy import build_trust_decision
from trust.preferences import preferences_for_email


STATS_FILE = Path("data/stats.json")
PROCESSED_LOG = Path("data/processed.json")
MAX_PROCESSED_IDS = 5000

LABEL_MAP = {
    "REJECTION": os.getenv("LABEL_REJECTION", "Job/Rejection"),
    "INTERVIEW": os.getenv("LABEL_INTERVIEW", "Job/Interview"),
    "HOLD": os.getenv("LABEL_HOLD", "Job/On-Hold"),
    "FOLLOW_UP": os.getenv("LABEL_FOLLOWUP", "Job/Follow-Up"),
    "APPLIED": os.getenv("LABEL_APPLIED", "Job/Applied"),
}

CATEGORY_COLOR = {
    "REJECTION": Fore.RED,
    "INTERVIEW": Fore.GREEN,
    "HOLD": Fore.YELLOW,
    "FOLLOW_UP": Fore.CYAN,
    "APPLIED": Fore.BLUE,
    "IRRELEVANT": Fore.WHITE,
}


def load_processed() -> set:
    """Load already-processed email IDs from disk."""
    if PROCESSED_LOG.exists():
        try:
            with open(PROCESSED_LOG, encoding="utf-8") as f:
                return set(json.load(f))
        except (json.JSONDecodeError, OSError) as e:
            logger.warning(f"Could not read processed log, starting fresh: {e}")
    return set()


def save_processed(ids: set) -> None:
    """Persist processed email IDs, trimming oldest entries if needed."""
    PROCESSED_LOG.parent.mkdir(exist_ok=True)
    id_list = list(ids)
    if len(id_list) > MAX_PROCESSED_IDS:
        id_list = id_list[-MAX_PROCESSED_IDS:]
    with open(PROCESSED_LOG, "w", encoding="utf-8") as f:
        json.dump(id_list, f, indent=2)


def _save_daily_stats(stats: dict, drafts: int, errors: int, queued: int = 0) -> None:
    """Append today's run statistics to a cumulative stats file."""
    today = datetime.now().strftime("%Y-%m-%d")
    all_stats = {}
    if STATS_FILE.exists():
        try:
            with open(STATS_FILE, encoding="utf-8") as f:
                all_stats = json.load(f)
        except (json.JSONDecodeError, OSError):
            pass

    day_entry = all_stats.get(
        today,
        {"runs": 0, "emails": {}, "drafts": 0, "queued": 0, "errors": 0},
    )
    day_entry["runs"] += 1
    day_entry["drafts"] += drafts
    day_entry["queued"] = day_entry.get("queued", 0) + queued
    day_entry["errors"] += errors
    for cat, count in stats.items():
        day_entry["emails"][cat] = day_entry["emails"].get(cat, 0) + count
    all_stats[today] = day_entry

    if len(all_stats) > 90:
        for key in sorted(all_stats.keys())[:-90]:
            del all_stats[key]

    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(all_stats, f, indent=2)


def _thread_has_draft(service, thread_id: str) -> bool:
    """Return True if a Gmail draft already exists for this thread."""
    if not thread_id:
        return False
    try:
        next_page_token = None
        while True:
            resp = service.users().drafts().list(
                userId="me",
                pageToken=next_page_token,
            ).execute()
            for draft in resp.get("drafts", []):
                if draft.get("message", {}).get("threadId") == thread_id:
                    return True
            next_page_token = resp.get("nextPageToken")
            if not next_page_token:
                break
    except Exception:
        logger.debug("Could not check existing Gmail drafts", exc_info=True)
    return False


def print_banner() -> None:
    now = datetime.now().strftime("%d %b %Y  %H:%M")
    print(f"\n{Fore.CYAN}{'=' * 60}")
    print("  MailAI Trust Agent")
    print(f"  {now}")
    print(f"{'=' * 60}{Style.RESET_ALL}\n")


def print_result(email: dict, result: dict, draft_saved: bool, queued: bool = False) -> None:
    cat = result.get("category", "IRRELEVANT")
    action = result.get("policy_action") or result.get("action", "SKIP")
    risk = result.get("risk_category", "FYI")
    confidence = result.get("confidence", 0)
    color = CATEGORY_COLOR.get(cat, Fore.WHITE)

    subject = email["subject"][:55]
    sender = email["sender"][:50]

    print(f"  {color}[{cat:<12}]{Style.RESET_ALL}  {subject}")
    print(f"               From:   {sender}")
    print(f"               Risk:   {risk} ({confidence})")
    print(f"               Action: {action}", end="")
    if draft_saved:
        print(f"  {Fore.GREEN}-> Draft saved{Style.RESET_ALL}", end="")
    if queued:
        print(f"  {Fore.YELLOW}-> Queued for review{Style.RESET_ALL}", end="")
    print("\n")


def run() -> None:
    logger.info("=" * 50)
    logger.info("MailAI run starting")
    print_banner()
    init_db()

    print(f"{Fore.CYAN}Authenticating with Gmail...{Style.RESET_ALL}")
    try:
        service = get_gmail_service()
        print(f"{Fore.GREEN}Connected to Gmail\n{Style.RESET_ALL}")
    except FileNotFoundError as e:
        print(e)
        logger.critical(str(e))
        sys.exit(1)
    except Exception as e:
        print(f"{Fore.RED}Auth failed: {e}{Style.RESET_ALL}")
        logger.exception("Auth failed")
        raise

    print(f"{Fore.CYAN}Setting up Gmail labels...{Style.RESET_ALL}")
    label_ids = {
        category: get_or_create_label(service, label_name)
        for category, label_name in LABEL_MAP.items()
    }
    print()

    days = int(os.getenv("SCAN_DAYS", "").strip() or 1)
    print(f"{Fore.CYAN}Fetching emails from last {days} day(s)...{Style.RESET_ALL}")
    emails = fetch_recent_emails(service, days=days)
    print(f"    Found {len(emails)} emails\n")
    logger.info(f"Fetched {len(emails)} emails for last {days} day(s)")

    if not emails:
        print(f"{Fore.YELLOW}    No emails to process.{Style.RESET_ALL}\n")
        logger.info("No emails to process. Exiting run.")
        return

    processed_ids = load_processed()
    stats = {k: 0 for k in ["REJECTION", "INTERVIEW", "HOLD", "FOLLOW_UP", "APPLIED", "IRRELEVANT"]}
    drafts_created = 0
    queued_for_review = 0
    skipped = 0
    errors = 0

    print(f"{Fore.CYAN}Processing emails with trust gates...{Style.RESET_ALL}\n  {'-' * 56}")

    db = SessionLocal()
    try:
        for email in emails:
            if email["id"] in processed_ids:
                skipped += 1
                continue

            max_retries = 3
            result = None
            for attempt in range(max_retries):
                try:
                    result = process_email(email)
                    break
                except Exception as e:
                    err_str = str(e).lower()
                    if "rate_limit" in err_str or "429" in err_str:
                        wait = 60 * (attempt + 1)
                        print(f"\n{Fore.YELLOW}Rate limit hit. Waiting {wait}s...{Style.RESET_ALL}")
                        logger.warning(f"Rate limit hit (attempt {attempt + 1}). Sleeping {wait}s.")
                        time.sleep(wait)
                    else:
                        logger.error(f"Error on attempt {attempt + 1} for email '{email['subject']}': {e}")
                        print(f"\n{Fore.RED}Error (attempt {attempt + 1}): {e}{Style.RESET_ALL}")
                        if attempt < max_retries - 1:
                            time.sleep(5)

            if not result:
                errors += 1
                processed_ids.add(email["id"])
                save_processed(processed_ids)
                continue

            category = result.get("category", "IRRELEVANT")
            stats[category] = stats.get(category, 0) + 1
            draft_saved = False
            queued = False

            try:
                preferences = preferences_for_email(db, email)
                decision = build_trust_decision(email, result, preferences)
                result.update(decision)
                outcome = execute_trust_decision(
                    db=db,
                    service=service,
                    email=email,
                    result=result,
                    decision=decision,
                    label_ids=label_ids,
                    thread_has_draft=_thread_has_draft,
                )
                draft_saved = bool(outcome.get("draft_saved"))
                queued = bool(outcome.get("queued"))
                if draft_saved:
                    drafts_created += 1
                if queued:
                    queued_for_review += 1
            except Exception as e:
                db.rollback()
                errors += 1
                logger.exception(f"Trust action execution failed for email '{email['subject'][:60]}': {e}")
                print(f"\n{Fore.RED}Trust action error: {e}{Style.RESET_ALL}")

            processed_ids.add(email["id"])
            save_processed(processed_ids)
            print_result(email, result, draft_saved, queued)
            logger.info(
                "[%s] action=%s policy=%s risk=%s subject='%s' draft=%s queued=%s",
                category,
                result.get("action", "SKIP"),
                result.get("policy_action", "SKIP"),
                result.get("risk_category", "FYI"),
                email["subject"][:60],
                draft_saved,
                queued,
            )

            time.sleep(float(os.getenv("MAILAI_PROCESSING_DELAY_SECONDS", "1.5")))
    finally:
        db.close()

    print(f"  {'-' * 56}\n")
    print(f"{Fore.CYAN}Summary{Style.RESET_ALL}")

    total_processed = sum(stats.values())
    for cat, count in stats.items():
        if count > 0:
            color = CATEGORY_COLOR.get(cat, Fore.WHITE)
            print(f"    {color}{cat:<14}{Style.RESET_ALL}  {count}")

    print(f"\n    {Fore.GREEN}Drafts saved:          {drafts_created}{Style.RESET_ALL}")
    print(f"    {Fore.YELLOW}Queued for review:     {queued_for_review}{Style.RESET_ALL}")
    print(f"    Skipped already done: {skipped}")
    if errors:
        print(f"    {Fore.RED}Emails with errors:    {errors}{Style.RESET_ALL}")
    print(f"\n{Fore.CYAN}Done. Review trust dashboard before sending anything.{Style.RESET_ALL}\n")

    logger.info(
        "Run complete. processed=%s, drafts=%s, queued=%s, skipped=%s, errors=%s",
        total_processed,
        drafts_created,
        queued_for_review,
        skipped,
        errors,
    )
    _save_daily_stats(stats, drafts_created, errors, queued_for_review)


if __name__ == "__main__":
    run()
