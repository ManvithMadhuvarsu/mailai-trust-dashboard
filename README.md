# MailAI — Trust Dashboard

**The first email agent built around controllable autonomy.**

Every action logged. Every draft gated. Every decision reversible.

[![Python 3.11+](https://img.shields.io/badge/Python-3.11%2B-3776AB?logo=python&logoColor=white)](https://www.python.org/)
[![Docker Ready](https://img.shields.io/badge/Docker-Ready-2496ED?logo=docker&logoColor=white)](https://www.docker.com/)
[![Railway Deploy](https://img.shields.io/badge/Railway-Deploy-0B0D0E?logo=railway&logoColor=white)](https://railway.app/)
[![LangGraph](https://img.shields.io/badge/LangGraph-Agent-FF6B35)](https://langchain-ai.github.io/langgraph/)

---

## Why This Exists

In February 2025, an AI email agent called OpenClaw was pointed at a real inbox. It started deleting every email older than a week. The owner typed "STOP OPENCLAW." It kept going. She had to run to her Mac mini to physically intervene. The post hit 9 million views on X.

That incident defined what the market actually needs: **not more autonomy — controllable autonomy with proof of what the agent did.**

Most "AI email agents" in 2026 fail not because they can't write, but because they act on the wrong things:
- Auto-replied to a boss's email at 2am
- Flagged a vendor invoice as spam and archived it
- Sent a follow-up before the draft was approved
- Applied a "ignore newsletters" rule to a client digest

MailAI solves this with a **Trust Layer** — a production control room that sits between the agent and your inbox.

---

## Architecture

```
Inbox → LangGraph Classifier → Confidence Gate → Policy Engine → Trust Decision
                                                                       │
                              ┌────────────────────────────────────────┤
                              │                                        │
                        Audit Log DB                           Review Queue
                        (every action)                    (human approval gate)
                              │
                        Trust Dashboard ← you
                        (approve / reject / undo)
```

**Core stack:** Gmail API + LangGraph + FastAPI + SQLAlchemy (SQLite / Postgres)

---

## Four Trust Modules

### Module 1 — Confidence Gate
- Classifies every email: `REJECTION / INTERVIEW / HOLD / FOLLOW_UP / APPLIED / IRRELEVANT`
- Risk overlay: `FINANCIAL / LEGAL / PERSONAL / VENDOR / JUNK / FYI / ACTION_REQUIRED`
- High-confidence FYI → auto-label; Low-confidence ACTION → surface to human
- Financial, legal, personal → **always** queue for review, never auto-act

### Module 2 — Audit Trail (the actual product)
- Every agent action logged: what email → what decision → why (cited context) → what action → reversible?
- **Undo last 5 actions** — full rollback on any reversible action in last 24h
- **Daily digest** — "Agent handled 47 emails. 3 queued for you. Here's what it did."
- **Weekly drift report** — category shifts >25% week-over-week flagged automatically

### Module 3 — Learned Preference Memory
- After each human correction, preference vector updated: `never_draft @domain.com`
- Auto-learns from repeated corrections (2+ rejections for same domain → blocked)
- **Weekly preference drift report** — "Your patterns changed — here's what the agent updated"
- **Export preferences as human-readable YAML** — `/api/preferences.yaml`

### Module 4 — Outbound Safety Layer
- Zero-shot send blocked by default for unknown domains
- Approval-gate mode: draft queued, sends **only** after human thumbs-up in dashboard
- **Tone classifier** — auto-flags drafts with >0.30 formality delta from your baseline
- Domain allowlist / blocklist with per-rule scope control

---

## Quick Start

```bash
pip install -r requirements.txt
cp .env.example .env
# Add GROQ_API_KEY + Gmail OAuth credentials
python main.py          # one-shot run
python daemon.py        # 24/7 continuous mode
```

### Trust Dashboard

```bash
uvicorn railway_app:app --host 0.0.0.0 --port 8080
# Open: http://localhost:8080/dashboard?key=YOUR_DASHBOARD_SECRET
```

Dashboard includes:
- **Review queue** — approval-gated replies waiting for your thumbs-up
- **Audit trail** — every decision with confidence, risk category, cited context
- **Undo controls** — rollback individual actions or last 5 in one click
- **Daily/weekly digest** — what the agent did, what patterns shifted
- **Preference rules** — YAML-exportable memory layer

---

## API Reference

| Method | Endpoint | Description |
|--------|----------|-------------|
| GET | `/dashboard` | Trust dashboard UI |
| GET | `/api/audit` | Recent audit events (limit param) |
| GET | `/api/review` | Review queue (queued/approved/rejected) |
| POST | `/api/review/{id}/approve` | Approve → create Gmail draft |
| POST | `/api/review/{id}/reject` | Reject + optional preference rule |
| POST | `/api/actions/{id}/undo` | Undo single reversible action |
| POST | `/api/actions/undo-last` | Bulk undo last N actions (default 5) |
| GET | `/api/digest/daily` | Today's stats |
| GET | `/api/digest/weekly` | 7-day stats + category drift report |
| GET | `/api/preferences` | All learned preference rules |
| POST | `/api/preferences` | Add rule manually |
| GET | `/api/preferences.yaml` | Export preferences as YAML |

---

## Gmail Categories + Labels

| Category | Gmail Label | Action |
|----------|-------------|--------|
| REJECTION | `Job/Rejection` | Draft feedback request |
| INTERVIEW | `Job/Interview` | Draft confirmation |
| HOLD | `Job/On-Hold` | Label only |
| FOLLOW_UP | `Job/Follow-Up` | Draft response |
| APPLIED | `Job/Applied` | Label only |
| IRRELEVANT | — | Skip |

---

## Configuration

See `.env.example` for all variables. Key ones:

```env
GROQ_API_KEY=...
YOUR_NAME=Your Full Name
YOUR_EMAIL=your@email.com
YOUR_LINKEDIN=linkedin.com/in/yourhandle

OUTBOUND_MODE=queue_review        # queue_review | gmail_draft
DASHBOARD_SECRET=long_random_secret
AUTO_ARCHIVE_ENABLED=false        # keep false until audit logs validate behavior
MIN_ACTION_CONFIDENCE=0.74
TONE_FLAG_THRESHOLD=0.30          # flag drafts deviating >30% from baseline tone
DATABASE_URL=                     # empty = SQLite; set for Postgres in production
```

---

## Deployment

### Docker

```bash
docker compose up -d --build
docker logs mailai-agent -f
```

### Railway

Start command:
```bash
sh -c "uvicorn railway_app:app --host 0.0.0.0 --port $PORT"
```

Required vars: `PUBLIC_BASE_URL`, `GMAIL_CREDENTIALS_JSON`, `GROQ_API_KEY`, `DASHBOARD_SECRET`

---

## Security

- Never commit `.env`, `config/credentials.json`, or `data/token.pickle`
- Set `DASHBOARD_SECRET` before any public deployment
- Agent **never auto-sends** — all outbound mail requires human approval or explicit `OUTBOUND_MODE=gmail_draft`
- Financial / Legal / Personal emails are hardcoded to `QUEUE_REVIEW` — no env variable can override this

---

## License

MIT
