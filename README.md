# MailAI

MailAI is an automated Gmail agent for job-search workflows. It classifies incoming emails, applies structured Gmail labels, and optionally creates draft replies.

The project supports local execution (Python), containerized execution (Docker), and cloud deployment (Railway).

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Railway Deploy](https://img.shields.io/badge/Railway-Deploy-0B0D0E?logo=railway&logoColor=white)](https://railway.app/)

## Quick Start (60 Seconds)

1. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
2. Create your env file:
   ```bash
   cp .env.example .env
   ```
3. Add `GROQ_API_KEY` in `.env`, then provide Gmail OAuth credentials via `config/credentials.json` or `GMAIL_CREDENTIALS_JSON`.
4. Run once to authorize Gmail:
   ```bash
   python main.py
   ```
5. Start continuous mode:
   ```bash
   python daemon.py
   ```

## Core Capabilities

- Classifies job-related emails into actionable categories
- Applies Gmail labels under a consistent `Job/*` taxonomy
- Generates approval-gated reply drafts (never auto-sends)
- Records every decision and Gmail action in the Trust Dashboard audit trail
- Queues risky or low-confidence mail for human review
- Learns correction preferences such as `never_draft` and `always_review`
- Runs continuously in daemon mode with configurable polling
- Supports Groq (cloud LLM) and Ollama (local LLM)
- Supports historical mailbox backfill without relabeling already-labeled messages

## Architecture

### Main Components

- `main.py`: one-shot scan and process pipeline
- `daemon.py`: continuous polling loop for 24/7 operation
- `agents/classifier_agent.py`: category and action decision logic
- `tools/gmail_tool.py`: Gmail OAuth, fetch, labels, drafts
- `trust/*`: confidence gates, audit DB, review queue, undo, dashboard APIs
- `web/templates` and `web/static`: production Trust Dashboard UI
- `backfill.py`: historical labeling pass with date windows
- `railway_app.py`: web OAuth + background loop for Railway deployments
- `tools/s3_state.py`: optional S3-compatible token persistence

### Data and State

- `config/credentials.json`: Google OAuth client credentials (not committed)
- `data/token.pickle`: Gmail OAuth token (generated at runtime)
- `data/processed.json`: processed message tracking

## Categories and Labeling

MailAI uses these categories:

- `REJECTION`
- `INTERVIEW`
- `HOLD`
- `FOLLOW_UP`
- `APPLIED`
- `IRRELEVANT`

Corresponding Gmail labels are created and managed under `Job/*`:

- `Job/Rejection`
- `Job/Interview`
- `Job/On-Hold`
- `Job/Follow-Up`
- `Job/Applied`

## Local Setup

1. Create and activate a Python 3.11+ environment.
2. Install dependencies:

```bash
pip install -r requirements.txt
```

3. Create your environment file:

```bash
cp .env.example .env
```

4. Provide Google OAuth credentials using one of:
   - `config/credentials.json` file
   - `GMAIL_CREDENTIALS_JSON` environment variable (full JSON)

5. Run once:

```bash
python main.py
```

## Run Modes

### One-Shot Processing

```bash
python main.py
```

### Continuous Daemon

```bash
python daemon.py
```

### Trust Dashboard

Start the FastAPI app and open `/dashboard`:

```bash
uvicorn railway_app:app --host 0.0.0.0 --port 8080
```

Set `DASHBOARD_SECRET` and unlock with:

```text
http://localhost:8080/dashboard?key=YOUR_DASHBOARD_SECRET
```

The dashboard includes:

- Review queue for approval-gated replies
- Audit trail with decision, confidence, risk category, and cited context
- Undo for reversible actions inside the 24-hour safety window
- Daily digest of handled, queued, and blocked high-risk emails
- Preference rules with YAML export at `/api/preferences.yaml`

### Backfill Historical Emails

```bash
python backfill.py
```

Backfill supports explicit date windows with:

- `BACKFILL_START_DATE` (`YYYY-MM-DD`)
- `BACKFILL_END_DATE` (`YYYY-MM-DD`)
- `BACKFILL_WINDOW_DAYS`
- `BACKFILL_MAX_PER_WINDOW`

## Docker Deployment

Build and run:

```bash
docker compose up -d --build
```

Key notes:

- OAuth callback defaults to port `8080`
- `config/credentials.json` is intentionally excluded from image build
- Use mounted `./config` or `GMAIL_CREDENTIALS_JSON`
- Data persists via mounted `./data`

View logs:

```bash
docker logs mailai-agent -f
```

Stop:

```bash
docker compose down
```

## Railway Deployment

### Service Start Command

Use:

```bash
sh -c "uvicorn railway_app:app --host 0.0.0.0 --port $PORT"
```

### Required Variables

- `PUBLIC_BASE_URL`
- `GMAIL_CREDENTIALS_JSON`
- `POLL_INTERVAL_MINUTES`
- LLM config (`GROQ_API_KEY` and/or Ollama settings)

### Optional Gmail Token Bootstrap

If `data/token.pickle` is not persisted yet, hosted environments can bootstrap
Gmail auth from:

- `GMAIL_TOKEN_PICKLE_B64`
- S3-compatible restore via `MAILAI_STATE_S3_*`

Startup order is:

1. Existing local `data/token.pickle`
2. S3 restore into `data/token.pickle`
3. `GMAIL_TOKEN_PICKLE_B64` materialized into `data/token.pickle`

### Google OAuth Configuration

Use a **Web application** OAuth client in Google Cloud and set:

- Authorized JavaScript origin: `https://<your-domain>`
- Authorized redirect URI: `https://<your-domain>/oauth/callback`

### Persistence Without Volumes

If persistent volumes are unavailable, enable S3-compatible token storage:

- `MAILAI_STATE_S3_ENABLED=true`
- `MAILAI_STATE_S3_ENDPOINT_URL=...`
- `MAILAI_STATE_S3_BUCKET=...`
- `MAILAI_STATE_S3_PREFIX=mailai`
- `AWS_ACCESS_KEY_ID=...`
- `AWS_SECRET_ACCESS_KEY=...`
- `AWS_REGION=auto`

If you do not want to use S3, you can instead set `GMAIL_TOKEN_PICKLE_B64`
to a base64-encoded `token.pickle` and the app will materialize it on startup.

## Configuration Reference

See `.env.example` for the complete, up-to-date variable list.

Important variables:

- Identity/signature: `YOUR_NAME`, `YOUR_PHONE`, `YOUR_EMAIL`, `YOUR_LINKEDIN`
- Runtime: `SCAN_DAYS`, `POLL_INTERVAL_MINUTES`
- Gmail OAuth: `GMAIL_CREDENTIALS_JSON`, `PUBLIC_BASE_URL`
- LLM: `USE_OLLAMA`, `OLLAMA_MODEL`, `OLLAMA_BASE_URL`, `GROQ_API_KEY`
- Backfill: `BACKFILL_*`
- Trust: `DATABASE_URL`, `DASHBOARD_SECRET`, `OUTBOUND_MODE`, `AUTO_ARCHIVE_ENABLED`
- Local UI testing: `MAILAI_DISABLE_DAEMON=true` renders the dashboard without starting inbox processing

## Security Best Practices

- Never commit `.env`, `config/credentials.json`, or token files
- Rotate credentials immediately if exposed
- Prefer least-privilege API keys
- Use separate OAuth clients for local and hosted environments

## Troubleshooting

### `redirect_uri_mismatch`

- OAuth client is incorrect type or missing hosted callback URI
- Use a Web OAuth client and set Railway callback URL exactly

### `invalid_grant` / missing PKCE verifier

- Ensure latest `railway_app.py` is deployed
- Restart login flow from `/login`

### `Could not restore data/token.pickle from S3 ... 403 Forbidden`

- Verify `MAILAI_STATE_S3_BUCKET`, `MAILAI_STATE_S3_ENDPOINT_URL`, and `MAILAI_STATE_S3_PREFIX`
- Verify `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY`
- Confirm the bucket policy allows read/write for `mailai/data/token.pickle`
- As a fallback, set `GMAIL_TOKEN_PICKLE_B64` so the app can boot without S3

### `credentials.json not found`

- Set `GMAIL_CREDENTIALS_JSON` correctly
- Or provide `config/credentials.json` in runtime filesystem

### Ollama not reachable from Docker

- Set `OLLAMA_BASE_URL` to a reachable host (for Docker on Windows often `http://host.docker.internal:11434`)

## License

Add your preferred license in a `LICENSE` file before public distribution.
