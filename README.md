# Lead Qualifier

Automated inbound lead qualification pipeline. A B2B contact form submission enters as a webhook POST, travels through validation → vector memory lookup → AI scoring → Google Sheets + Slack, and returns a structured JSON result to the caller — all in a single synchronous round-trip.

---

## Architecture

```
                     ┌─────────────────────────────────────────┐
                     │           Docker Compose                 │
  POST /webhook/lead │                                          │
  ─────────────────► │  ┌──────────────────┐                   │
                     │  │  app  (FastAPI)   │◄─────────────────►│── Groq API
  JSON response ◄─── │  │  port 8000        │                   │   (LLaMA 3.3 70B)
                     │  └────────┬─────────┘                   │
                     │           │                              │
                     │           ▼                              │
                     │  ┌──────────────────┐                   │
                     │  │  qdrant           │                   │
                     │  │  port 6333        │                   │
                     │  │  (vector memory)  │                   │
                     │  └──────────────────┘                   │
                     └─────────────────────────────────────────┘
                                  │
                     ┌────────────┴────────────┐
                     ▼                         ▼
              Google Sheets              Slack Webhook
              (record store)           (team notification)
```

### Pipeline stages

```
POST /webhook/lead
  │
  ├─[1-3] Validate + Normalize ── missing field? ──► HTTP 400 + Slack alert
  │
  ├─[3]   Message < 10 chars? ──────────────────────► Slack lightweight notice + Cold response
  │
  ├─[4]   Vector memory lookup (Qdrant) ── similar past lead found? ── attach prior context
  │
  ├─[5]   AI scoring (Groq — LLaMA 3.3 70B, JSON mode)
  │          API error / timeout / bad response ──────► fallback result, pipeline continues
  │
  ├─[6]   Store embedding in Qdrant
  │
  ├─[7a]  Append row → Google Sheets
  ├─[7b]  Slack notification (Hot → #sales-urgent + @here │ Warm/Cold → #sales-leads)
  │
  └──────► Return { priority_tier, lead_score, intent_summary, suggested_opener }
```

---

## Tech Stack

| Component | Technology | Why |
|---|---|---|
| Web framework | FastAPI + Uvicorn | Async, typed, auto-generates `/docs` for demo |
| AI scoring | Groq (`llama-3.3-70b-versatile`) | Free tier, 14 400 req/day, JSON mode enforces output schema |
| Vector memory | Qdrant (Docker) | Persistent named volume, no external service needed |
| Embeddings | `all-MiniLM-L6-v2` (sentence-transformers) | Runs on CPU inside Docker — no GPU, no extra API key |
| Record store | Google Sheets via gspread | Zero infrastructure, easy to demo and share |
| Notifications | Slack Incoming Webhooks | Two webhooks: urgent channel for Hot, default for Warm/Cold |
| Config | pydantic-settings | Typed env vars, single source of truth, `.env` file support |

---

## Quick Start

### Prerequisites

- Docker + Docker Compose installed
- A [Groq](https://console.groq.com) account (free, no credit card) — for AI scoring
- A Google Cloud service account JSON with Sheets + Drive APIs enabled — for writing leads
- Two Slack Incoming Webhook URLs — for notifications

### 1. Clone and enter the project

```bash
git clone <your-repo-url>
cd lead-qualifier
```

### 2. Configure environment variables

```bash
cp .env.example .env
```

Open `.env` and fill in every value:

| Variable | Where to get it |
|---|---|
| `GROQ_API_KEY` | [console.groq.com](https://console.groq.com) → API Keys → Create API key |
| `GOOGLE_SERVICE_ACCOUNT_JSON` | Path to your service account JSON file (default: `/app/google_credentials.json`) |
| `GOOGLE_SHEET_ID` | The ID from your Google Sheet URL: `spreadsheets/d/<ID>/edit` |
| `SLACK_WEBHOOK_URL_DEFAULT` | [api.slack.com/apps](https://api.slack.com/apps) → your app → Incoming Webhooks |
| `SLACK_WEBHOOK_URL_URGENT` | Second webhook for your `#sales-urgent` channel |

### 3. Add your Google credentials

Place your service account JSON file in the project root:

```bash
mv ~/Downloads/your-service-account-key.json ./google_credentials.json
```

The service account email must have **Editor** access to your Google Sheet.  
Enable both **Google Sheets API** and **Google Drive API** in your Google Cloud project.

### 4. Set up the Google Sheet

Create a blank Google Sheet, share it with your service account email (Editor role), then run the setup script to write the headers:

```bash
docker compose run --rm --no-deps app python create_sheet.py
```

Copy the `GOOGLE_SHEET_ID` it prints into your `.env`.

### 5. Start all services

```bash
docker compose up --build
```

First build takes 2–3 minutes (downloads and pre-bakes the embedding model). Subsequent starts are fast.

### 6. Open the demo UI

Go to **http://localhost:8000** — a form UI with 4 scenario presets lets you test every pipeline branch interactively.

Or send a raw curl request:

```bash
curl -X POST http://localhost:8000/webhook/lead \
  -H "Content-Type: application/json" \
  -d '{
    "full_name": "Jordan Ellis",
    "email": "jordan.ellis@examplecorp.com",
    "company_name": "Example Corp",
    "job_title": "Operations Manager",
    "company_size": "11-50",
    "budget_range": "$5k-$15k/mo",
    "message": "We are drowning in manual data entry across three tools and need to automate onboarding before headcount doubles next quarter."
  }'
```

The interactive API docs are at **http://localhost:8000/docs**.

### Test Scenarios

| Scenario | What to send | Expected outcome |
|---|---|---|
| High-intent lead | Full payload, detailed message | `Hot` or `Warm`, score 70–100, Slack @here |
| Missing message | `"message": ""` | HTTP 400, Slack incomplete-lead alert |
| Short message | `"message": "hi"` | `Cold`, no AI call, Slack low-context notice |
| Spam / student | Student research request | `Cold`, score < 30, red flags populated |

---

## AI Prompt Design

### System prompt (fixed, sent on every request)

```
You are a B2B sales qualification assistant. Evaluate inbound leads and score
them based on fit, urgency, and buying intent.

Scoring guidelines:
- Hot  (score 70–100): Clear business pain, specific budget, decision-maker title, urgent timeline
- Warm (score 40–69):  Real business interest but missing one or more strong buying signals
- Cold (score 1–39):   Vague inquiry, student/researcher, spam, competitor, or no budget signal

If the message reads like spam, a student project, competitor research, or
generic curiosity with no business need, score it Cold and populate red_flags accordingly.

You MUST respond with a single valid JSON object matching this exact schema —
no extra keys, no prose outside the JSON:
{
  "lead_score": <integer 1–100>,
  "priority_tier": <"Hot" | "Warm" | "Cold">,
  "intent_summary": <one sentence describing what the lead actually needs>,
  "suggested_opener": <one personalised first sentence to open a sales reply>,
  "red_flags": <array of short strings, empty array if none>
}
```

### User message (dynamic, built per lead)

```
Lead Details:
- Name:         Jordan Ellis
- Job Title:    Operations Manager
- Company:      Example Corp
- Company Size: 11-50
- Budget Range: $5k-$15k/mo
- Message:      We are drowning in manual data entry...

# When a similar past lead is found in Qdrant:
Prior Context (similar past lead detected — similarity: 0.91):
A previous lead from 'Example Corp' scored 82 and was rated 'Hot'.
Factor this into your analysis — this may be a returning or repeat inquiry.
```

### Structured output (enforced via JSON mode)

`response_format={"type": "json_object"}` is set on every Groq request. The model is constrained to return valid JSON. The system prompt further constrains the shape to match our exact schema:

| Field | Type | Description |
|---|---|---|
| `lead_score` | integer 1–100 | Overall fit and intent score |
| `priority_tier` | `"Hot"` \| `"Warm"` \| `"Cold"` | Enum-constrained sales tier |
| `intent_summary` | string | One sentence: what the lead actually wants |
| `suggested_opener` | string | Personalised first email line |
| `red_flags` | string[] | Concerns (spam, no budget, etc.) or empty array |

`temperature=0` is used throughout — same lead scores the same way on every run.

---

## Key Design Decisions

| Decision | Reasoning |
|---|---|
| JSON mode (`response_format`) | Enforces the output schema at the API level. The model cannot return markdown or prose instead of the structured object. |
| `temperature=0` | Same lead should score the same way on every run. Determinism matters for a qualification system. |
| Local embeddings (`all-MiniLM-L6-v2`) | No extra API key needed. Runs on CPU inside the Docker container. Model is baked into the image at build time so startup is instant — no 30s cold-start per deploy. |
| Validation before embedding and AI | A lead with a missing email or blank message never touches Qdrant or the Groq API — no wasted cost or latency. |
| Qdrant in Docker with named volume | Vector memory persists across container restarts. No external managed service required for development or demo. |
| Two Slack webhooks (urgent + default) | Hot leads need immediate attention. A single channel with identical formatting makes hot leads easy to miss. |
| Non-fatal outputs | Google Sheets and Slack failures are caught and logged. The pipeline always completes and always returns a result — it degrades gracefully rather than crashing. |

---

## Extension A — Vector Memory

Before each AI call, the lead's message is embedded and compared against all previous leads stored in Qdrant (cosine similarity). If a match scores above **0.82**, the prior lead's company, score, and tier are injected into the AI prompt as context.

**Why 0.82?** High enough to avoid false positives from different companies using similar business language; low enough to catch genuine returning inquiries from the same organisation. The threshold is configurable via `SIMILARITY_THRESHOLD` in `.env`.

**What is embedded:** only the `message` field — the semantic signal. Name, company, score, and tier are stored as Qdrant payload metadata for retrieval but are not part of the vector.

---

## Known Limitations

- **No deduplication by email.** The same email address can be submitted multiple times and each submission is treated as a new lead. In production, an upsert-by-email pattern would prevent duplicate Sheet rows.
- **Similarity threshold is asserted, not calibrated.** The 0.82 value is a reasonable starting point but has not been validated against a labelled dataset of real leads.
- **Google Sheets credentials are loaded on every request.** The gspread auth round-trip adds ~200ms per lead. Acceptable for low volume; a cached client would be needed at scale.
- **Single-worker Uvicorn.** The Docker setup runs one process. Under concurrent load, the synchronous embedding call and gspread I/O would block. Production would need `--workers N` or an async gspread client.
- **No retry logic on Slack or Sheets.** A transient network error silently drops the notification. A simple exponential backoff would fix this.
- **Groq free tier rate limits.** 14 400 requests/day and 6 000 tokens/minute. Sufficient for demo and light production; a paid plan or request queuing would be needed at volume.

---

## What I'd Improve With More Time

1. **Email deduplication** — upsert-by-email in Sheets; skip re-embedding leads already in Qdrant.
2. **Calibrate the similarity threshold** — run the four test payloads against a small labelled set and tune to minimise false positives.
3. **Cache external clients** — gspread client created once at startup, not per request.
4. **Add retry logic** — exponential backoff on Slack and Sheets writes.
5. **JSON-structured logs** — swap the log format for `python-json-logger` so log aggregators can filter by `priority_tier`, `lead_score`, etc.
6. **Unit tests for the validator and fallback logic** — the pipeline branches are deterministic and straightforward to cover.
