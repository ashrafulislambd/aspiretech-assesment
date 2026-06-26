# Lead Qualifier

Automated inbound lead qualification pipeline. A B2B contact form submission enters as a webhook POST, travels through validation → vector memory lookup → AI scoring → Google Sheets + Slack, and returns a structured JSON result to the caller — all in a single synchronous round-trip.

---

## Architecture

```
                     ┌─────────────────────────────────────────┐
                     │           Docker Compose                 │
  POST /webhook/lead │                                          │
  ─────────────────► │  ┌──────────────────┐                   │
                     │  │  app  (FastAPI)   │◄─────────────────►│── Anthropic API
  JSON response ◄─── │  │  port 8000        │                   │   (Claude Haiku)
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
  ├─[5]   AI scoring (Claude Haiku via tool_use)
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
| AI scoring | Google Gemini Flash (`gemini-2.0-flash`) | Fast, cheap, free tier available; enforces output schema via `response_schema` |
| Vector memory | Qdrant (Docker) | Persistent named volume, no external service needed |
| Embeddings | `all-MiniLM-L6-v2` (sentence-transformers) | Runs on CPU inside Docker — no GPU, no extra API key |
| Record store | Google Sheets via gspread | Zero infrastructure, easy to demo and share |
| Notifications | Slack Incoming Webhooks | Two webhooks: urgent channel for Hot, default for Warm/Cold |
| Config | pydantic-settings | Typed env vars, single source of truth, `.env` file support |

---

## Quick Start

```bash
# 1. Clone and enter the project
cd lead-qualifier

# 2. Copy environment template and fill in your credentials
cp .env.example .env

# 3. Place your Google service account JSON in the project root
#    (see .env.example for setup instructions)
mv ~/Downloads/your-key.json ./google_credentials.json

# 4. Build and start all services
docker-compose up --build

# 5. Send a test lead
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

The API docs are available at `http://localhost:8000/docs` once running.

---

## AI Prompt Design

### System prompt (fixed, sent on every request)

```
You are a B2B sales qualification assistant. Your job is to evaluate inbound
leads and score them based on their fit, urgency, and buying intent.

Scoring guidelines:
- Hot  (score 70–100): Clear business pain, specific budget, decision-maker title, urgent timeline
- Warm (score 40–69):  Real business interest but missing one or more strong buying signals
- Cold (score 1–39):   Vague inquiry, student/researcher, spam, competitor, or no budget signal

If the message reads like spam, a student project, competitor research, or
generic curiosity with no business need, score it Cold and populate red_flags accordingly.

Always call the score_lead tool to return your analysis. Do not write any prose outside the tool call.
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

### Structured output (enforced via `response_schema`)

`response_mime_type="application/json"` and `response_schema=LeadScoreOutput` are set on every request. Gemini is constrained to return a JSON object matching the schema — it cannot return free-form text. Fields returned:

| Field | Type | Description |
|---|---|---|
| `lead_score` | integer 1–100 | Overall fit and intent score |
| `priority_tier` | `"Hot"` \| `"Warm"` \| `"Cold"` | Enum-constrained sales tier |
| `intent_summary` | string | One sentence: what the lead actually wants |
| `suggested_opener` | string | 1–2 sentence personalised first email line |
| `red_flags` | string[] | Concerns (spam, no budget, etc.) or empty array |

`temperature=0` is used throughout — this is a scoring task, not a creative one.

---

## Key Design Decisions

| Decision | Reasoning |
|---|---|
| `response_schema` (Gemini JSON mode) | Enforces the output schema at the API level. The model cannot return markdown or prose instead of the structured object. |
| `temperature=0` | Same lead should score the same way on every run. Determinism matters for a qualification system. |
| Local embeddings (`all-MiniLM-L6-v2`) | No extra API key needed alongside Anthropic. Runs on CPU inside the Docker container. Model is baked into the image at build time so startup is instant. |
| Validation before embedding and AI | A lead with a missing email or blank message never touches Qdrant or the Anthropic API — no wasted cost or latency. |
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

---

## What I'd Improve With More Time

1. **Email deduplication** — upsert-by-email in Sheets; skip re-embedding leads already in Qdrant.
2. **Calibrate the similarity threshold** — run the four test payloads against a small labelled set and tune to minimise false positives.
3. **Cache external clients** — Anthropic and gspread clients created once at startup, not per request.
4. **Add retry logic** — exponential backoff on Slack and Sheets writes.
5. **JSON-structured logs** — swap the log format for `python-json-logger` so log aggregators can filter by `priority_tier`, `lead_score`, etc.
6. **Unit tests for the validator and fallback logic** — the pipeline branches are deterministic and straightforward to cover.
