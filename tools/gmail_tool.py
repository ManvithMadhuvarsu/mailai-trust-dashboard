"""
tools/gmail_tool.py
Handles all Gmail API interactions:
- OAuth authentication with automatic token refresh and re-auth
- Reading emails (plain text + HTML fallback)
- Creating/applying labels
- Saving drafts
"""

import os
import re
import base64
import pickle
import logging
from pathlib import Path
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

logger = logging.getLogger(__name__)

# Gmail API scopes
SCOPES = [
    "https://www.googleapis.com/auth/gmail.readonly",
    "https://www.googleapis.com/auth/gmail.modify",
    "https://www.googleapis.com/auth/gmail.compose",
    "https://www.googleapis.com/auth/gmail.labels",
]

TOKEN_PATH = Path("data/token.pickle")
CREDENTIALS_PATH = Path("config/credentials.json")


def _materialize_credentials_from_env() -> None:
    """If config/credentials.json is missing, write it from GMAIL_CREDENTIALS_JSON (Docker/Railway)."""
    if CREDENTIALS_PATH.exists():
        return
    raw = os.getenv("GMAIL_CREDENTIALS_JSON", "").strip()
    if not raw:
        return
    CREDENTIALS_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CREDENTIALS_PATH, "w", encoding="utf-8") as f:
        f.write(raw)
    logger.info("Wrote config/credentials.json from GMAIL_CREDENTIALS_JSON environment variable.")


def _is_headless_runtime() -> bool:
    """Return True when running in a non-interactive/headless environment."""
    # Docker containers are typically headless for OAuth browser flow.
    if Path("/.dockerenv").exists():
        return True
    if os.getenv("MAILAI_HEADLESS_AUTH", "").strip().lower() in {"1", "true", "yes"}:
        return True
    return not bool(os.getenv("DISPLAY"))


def _run_oauth_flow(flow: InstalledAppFlow):
    """Run OAuth flow with headless-friendly behavior for containers."""
    if _is_headless_runtime():
        oauth_port = int(os.getenv("GMAIL_OAUTH_LOCAL_PORT", "").strip() or 8080)
        oauth_public_host = os.getenv("GMAIL_OAUTH_PUBLIC_HOST", "").strip() or "localhost"
        print("\n  🌐 First-time Gmail authorization required.")
        print(f"  👉 Open the Google auth URL from logs in your browser and complete sign-in.")
        print(f"  🔌 Waiting for callback on {oauth_public_host}:{oauth_port}...")
        return flow.run_local_server(
            host=oauth_public_host,
            bind_addr="0.0.0.0",
            port=oauth_port,
            open_browser=False,
        )
    return flow.run_local_server(port=0)


def save_token_pickle(creds) -> None:
    """Persist OAuth credentials to TOKEN_PATH."""
    TOKEN_PATH.parent.mkdir(exist_ok=True)
    with open(TOKEN_PATH, "wb") as f:
        pickle.dump(creds, f)


def _load_env_token_pickle():
    """Decode credentials from GMAIL_TOKEN_PICKLE_B64, if present and valid."""
    token_b64 = os.getenv("GMAIL_TOKEN_PICKLE_B64", "").strip()
    if not token_b64:
        return None
    try:
        creds_data = base64.b64decode(token_b64)
        return pickle.loads(creds_data)
    except Exception as e:
        logger.error(f"Failed to decode GMAIL_TOKEN_PICKLE_B64: {e}")
        return None


def materialize_token_pickle_from_env() -> bool:
    """
    Persist an env-backed token to TOKEN_PATH when the file is missing.

    This lets hosted runtimes boot from GMAIL_TOKEN_PICKLE_B64 even when
    persistent file restore is unavailable.
    """
    if TOKEN_PATH.exists():
        return False
    creds = _load_env_token_pickle()
    if not creds:
        return False
    save_token_pickle(creds)
    logger.info("Wrote data/token.pickle from GMAIL_TOKEN_PICKLE_B64.")
    return True


def get_gmail_service():
    """Authenticate and return Gmail API service.

    Handles token refresh automatically. If the refresh token has been
    revoked or expired (common for Google OAuth 'Testing' apps which
    expire tokens every 7 days), the stale token is deleted and a fresh
    OAuth flow is triggered.
    """
    _materialize_credentials_from_env()

    creds = None
    loaded_from_env_token = False
    
    # ── Check for TOKEN_B64 from Environment (Render/Cloud Support) ────────────
    token_b64 = os.getenv("GMAIL_TOKEN_PICKLE_B64")
    if token_b64:
        try:
            logger.info("Found GMAIL_TOKEN_PICKLE_B64 in environment.")
            creds_data = base64.b64decode(token_b64)
            creds = pickle.loads(creds_data)
            loaded_from_env_token = True
        except Exception as e:
            logger.error(f"Failed to decode GMAIL_TOKEN_PICKLE_B64: {e}")

    # ── Fallback to local file ────────────────────────────────────────────────
    if not creds and TOKEN_PATH.exists():
        with open(TOKEN_PATH, "rb") as f:
            creds = pickle.load(f)

    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                logger.info("Gmail token refreshed successfully.")
            except Exception as e:
                err_text = str(e)
                # Token was revoked/expired beyond refresh.
                logger.warning(f"Token refresh failed: {e}.")
                print(f"  ⚠️  Token refresh failed: {e}")
                TOKEN_PATH.unlink(missing_ok=True)
                creds = None

                # If stale token comes from env, reauth cannot be done automatically in daemon mode.
                if loaded_from_env_token and "invalid_grant" in err_text.lower():
                    raise RuntimeError(
                        "Google OAuth refresh token is invalid (invalid_grant). "
                        "Regenerate token.pickle interactively (run `python main.py` once), "
                        "then update GMAIL_TOKEN_PICKLE_B64 with the new token."
                    ) from e

        if not creds:
            if not CREDENTIALS_PATH.exists():
                raise FileNotFoundError(
                    "\n❌  credentials.json not found!\n"
                    "    Option A — Local / Docker bind mount:\n"
                    "      Save your Google OAuth client JSON as: config/credentials.json\n"
                    "    Option B — Environment variable (Docker/Railway):\n"
                    "      Set GMAIL_CREDENTIALS_JSON to the full JSON string (same file contents).\n"
                    "    Google Cloud setup:\n"
                    "    1. Go to https://console.cloud.google.com\n"
                    "    2. Create a project → Enable Gmail API\n"
                    "    3. Create OAuth 2.0 credentials (Desktop app)\n"
                    "    4. Download the JSON and use as above\n"
                )
            flow = InstalledAppFlow.from_client_secrets_file(
                str(CREDENTIALS_PATH), SCOPES,
            )
            creds = _run_oauth_flow(flow)
            logger.info("New Gmail OAuth token obtained.")

        # Always try to persist new token to file if possible
        try:
            save_token_pickle(creds)
        except Exception:
            pass # Ignore if disk is read-only (common in some cloud environments)

    return build("gmail", "v1", credentials=creds)


def _html_to_text(html: str) -> str:
    """Strip HTML tags and decode common entities to plain text."""
    # Remove style and script blocks entirely
    html = re.sub(r"<(style|script)[^>]*>.*?</(style|script)>", "", html, flags=re.DOTALL | re.IGNORECASE)
    # Replace block-level tags with newlines for readability
    html = re.sub(r"<(br|p|div|tr|li)[^>]*>", "\n", html, flags=re.IGNORECASE)
    # Remove remaining tags
    html = re.sub(r"<[^>]+>", "", html)
    # Decode common HTML entities
    entities = {"&amp;": "&", "&lt;": "<", "&gt;": ">", "&nbsp;": " ", "&quot;": '"', "&#39;": "'"}
    for entity, char in entities.items():
        html = html.replace(entity, char)
    # Collapse excessive whitespace
    html = re.sub(r"\n{3,}", "\n\n", html)
    html = re.sub(r"[ \t]+", " ", html)
    return html.strip()


def _decode_part(part: dict) -> str:
    """Safely decode a base64url-encoded email body part."""
    data = part.get("body", {}).get("data", "")
    if not data:
        return ""
    try:
        return base64.urlsafe_b64decode(data).decode("utf-8", errors="replace")
    except Exception:
        return ""


def _extract_body(payload: dict) -> str:
    """
    Recursively extract the best available body from an email payload.
    Priority: text/plain > text/html > any text part.
    """
    mime = payload.get("mimeType", "")

    # Leaf node
    if "parts" not in payload:
        if mime == "text/plain":
            return _decode_part(payload)
        if mime == "text/html":
            return _html_to_text(_decode_part(payload))
        return ""

    plain, html = "", ""
    for part in payload["parts"]:
        sub_mime = part.get("mimeType", "")
        if sub_mime == "text/plain":
            plain = _decode_part(part)
        elif sub_mime == "text/html":
            html = _html_to_text(_decode_part(part))
        elif sub_mime.startswith("multipart/"):
            # Recurse into nested multipart containers
            nested = _extract_body(part)
            if nested:
                plain = plain or nested

    return plain or html


def fetch_recent_emails(service, days: int = 1) -> list:
    """Fetch emails from the last N days from the inbox."""
    since = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
    query = f"after:{since} in:inbox"
    return fetch_emails_by_query(service, query=query, max_total=500)


def fetch_emails_by_query(service, query: str, max_total: int = 500) -> list:
    """Fetch up to max_total emails matching a Gmail search query."""

    try:
        messages = []
        next_page_token = None

        while True:
            results = service.users().messages().list(
                userId="me",
                q=query,
                maxResults=100,
                pageToken=next_page_token,
            ).execute()

            batch = results.get("messages", [])
            messages.extend(batch)
            next_page_token = results.get("nextPageToken")

            # Safety cap to avoid infinite loops or memory issues in very large mailboxes
            if not next_page_token or len(messages) >= max_total:
                break

        emails = []
        for msg in messages:
            try:
                detail = service.users().messages().get(
                    userId="me", id=msg["id"], format="full"
                ).execute()
                emails.append(_parse_email(detail))
            except HttpError as e:
                logger.warning(f"Failed to fetch email {msg['id']}: {e}")

        return emails

    except HttpError as e:
        logger.error(f"Gmail API error while listing messages: {e}")
        print(f"❌ Gmail API error: {e}")
        return []


def _parse_email(raw: dict) -> dict:
    """Parse raw Gmail message into a clean, normalized dict."""
    headers = {
        h["name"].lower(): h["value"]
        for h in raw["payload"].get("headers", [])
    }

    body = _extract_body(raw["payload"])

    # Truncate body to ~4000 chars to avoid LLM token overflow while keeping context
    body = body[:4000].strip()

    # Extract sender name and address separately
    sender_raw = headers.get("from", "")
    sender_name = re.sub(r"<.*?>", "", sender_raw).strip().strip('"')
    sender_email = re.search(r"<(.+?)>", sender_raw)
    sender_email = sender_email.group(1) if sender_email else sender_raw.strip()

    return {
        "id": raw["id"],
        "thread_id": raw["threadId"],
        "subject": headers.get("subject", "(no subject)").strip(),
        "sender": sender_raw,
        "sender_name": sender_name,
        "sender_email": sender_email,
        "reply_to": headers.get("reply-to", sender_raw),
        "date": headers.get("date", ""),
        "body": body,
        "label_ids": raw.get("labelIds", []),
        "snippet": raw.get("snippet", ""),
    }


def get_or_create_label(service, label_name: str) -> str | None:
    """Get label ID by name, creating it if it doesn't exist."""
    try:
        existing = service.users().labels().list(userId="me").execute()
        for label in existing.get("labels", []):
            if label["name"] == label_name:
                return label["id"]

        new_label = service.users().labels().create(
            userId="me",
            body={
                "name": label_name,
                "labelListVisibility": "labelShow",
                "messageListVisibility": "show",
            },
        ).execute()
        logger.info(f"Created Gmail label: {label_name}")
        print(f"  ✅ Created Gmail label: {label_name}")
        return new_label["id"]

    except HttpError as e:
        logger.error(f"Label error for '{label_name}': {e}")
        print(f"❌ Label error: {e}")
        return None


def apply_label(service, message_id: str, label_id: str) -> bool:
    """Apply a label to a Gmail message. Returns True on success."""
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": [label_id]},
        ).execute()
        return True
    except HttpError as e:
        logger.warning(f"Failed to apply label to {message_id}: {e}")
        print(f"❌ Failed to apply label: {e}")
        return False


def remove_label(service, message_id: str, label_id: str) -> bool:
    """Remove a label from a Gmail message. Returns True on success."""
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": [label_id]},
        ).execute()
        return True
    except HttpError as e:
        logger.warning(f"Failed to remove label from {message_id}: {e}")
        print(f"Failed to remove label: {e}")
        return False


def archive_message(service, message_id: str) -> bool:
    """Archive a Gmail message by removing it from Inbox."""
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"removeLabelIds": ["INBOX"]},
        ).execute()
        return True
    except HttpError as e:
        logger.warning(f"Failed to archive {message_id}: {e}")
        print(f"Failed to archive message: {e}")
        return False


def restore_message_to_inbox(service, message_id: str) -> bool:
    """Restore an archived Gmail message to Inbox."""
    try:
        service.users().messages().modify(
            userId="me",
            id=message_id,
            body={"addLabelIds": ["INBOX"]},
        ).execute()
        return True
    except HttpError as e:
        logger.warning(f"Failed to restore {message_id} to inbox: {e}")
        print(f"Failed to restore message: {e}")
        return False


def delete_draft(service, draft_id: str) -> bool:
    """Delete a Gmail draft by ID. Returns True on success."""
    if not draft_id:
        return True
    try:
        service.users().drafts().delete(userId="me", id=draft_id).execute()
        logger.info(f"Draft deleted: {draft_id}")
        return True
    except HttpError as e:
        logger.warning(f"Failed to delete draft {draft_id}: {e}")
        print(f"Failed to delete draft: {e}")
        return False


def save_draft(service, to: str, subject: str, body: str, thread_id: str = None) -> str | None:
    """Save an email as a Gmail draft (does NOT send automatically)."""
    try:
        msg = MIMEMultipart("alternative")
        msg["To"] = to
        msg["Subject"] = subject
        msg.attach(MIMEText(body, "plain", "utf-8"))

        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft_body = {"message": {"raw": raw}}
        if thread_id:
            draft_body["message"]["threadId"] = thread_id

        draft = service.users().drafts().create(
            userId="me", body=draft_body
        ).execute()
        logger.info(f"Draft saved: subject='{subject}' to='{to}'")
        return draft["id"]

    except HttpError as e:
        logger.error(f"Failed to save draft to '{to}': {e}")
        print(f"❌ Failed to save draft: {e}")
        return None
