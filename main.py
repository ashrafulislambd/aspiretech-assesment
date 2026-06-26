import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from qdrant_client import QdrantClient

from config import get_settings
from models import AIResult, LeadInput, WebhookResponse
from pipeline.ai_analyzer import analyze_lead
from pipeline.outputs import (
    append_lead_to_sheet,
    send_incomplete_lead_alert,
    send_low_context_alert,
    send_qualified_lead_notification,
)
from pipeline.validator import validate_and_normalize
from pipeline.vector_memory import (
    embed_message,
    ensure_collection_exists,
    find_similar_lead,
    get_qdrant_client,
    store_lead_embedding,
)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-8s  %(name)s — %(message)s",
)
logger = logging.getLogger(__name__)

settings = get_settings()

# Qdrant client is created once at startup and shared across all requests
qdrant_client: QdrantClient | None = None


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Runs setup code before the server starts accepting requests."""
    global qdrant_client
    qdrant_client = get_qdrant_client(settings.qdrant_host, settings.qdrant_port)
    ensure_collection_exists(qdrant_client)
    logger.info("Startup complete — Qdrant connected, collection ready.")
    yield
    # Nothing to tear down; the Qdrant client is stateless


app = FastAPI(title="Lead Qualifier API", version="1.0.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory="static"), name="static")


@app.get("/", include_in_schema=False)
async def serve_frontend() -> FileResponse:
    """Serves the demo frontend at the root URL."""
    return FileResponse("static/index.html")


@app.post("/webhook/lead", response_model=WebhookResponse)
async def receive_lead(lead: LeadInput) -> WebhookResponse:
    """
    Main webhook endpoint. Accepts a raw lead payload, runs it through the
    full qualification pipeline, and returns the AI result to the caller.

    Pipeline stages:
      1. Validate required fields
      2. Normalize data
      3. Branch on message length (low-context skips AI)
      4. Vector memory lookup (Extension A)
      5. AI scoring via Claude Haiku
      6. Store embedding in Qdrant
      7a. Append row to Google Sheets
      7b. Send Slack notification
    """
    logger.info(f"Received lead from: {lead.email!r}")

    # ── Stages 1–3: Validate and normalize ────────────────────────────────────
    validation_result = validate_and_normalize(lead, settings.min_message_length)

    if not validation_result.is_valid:
        logger.warning(f"Validation failed: {validation_result.error_reason}")
        _alert_incomplete_lead(lead.email, validation_result.error_reason)
        raise HTTPException(status_code=400, detail=validation_result.error_reason)

    clean_lead = validation_result.normalized_lead

    if validation_result.is_low_context:
        logger.info(f"Low-context message ({len(clean_lead.message)} chars) — skipping AI scoring.")
        _alert_low_context_lead(clean_lead)
        return WebhookResponse(
            status="low_context",
            priority_tier="Cold",
            lead_score=0,
            intent_summary="Message too short to qualify automatically.",
            suggested_opener="Reach out asking for more details about their specific needs.",
            red_flags=[],
            prior_context_used=False,
            manual_review=False,
        )

    # ── Stage 4: Vector memory lookup ─────────────────────────────────────────
    message_embedding = embed_message(clean_lead.message)
    prior_context = _lookup_similar_lead(message_embedding)

    # ── Stage 5: AI scoring ───────────────────────────────────────────────────
    logger.info("Calling Claude Haiku to score the lead.")
    ai_result = analyze_lead(clean_lead, prior_context, settings.groq_api_key)
    ai_result.prior_context_used = prior_context is not None
    logger.info(f"AI result — tier: {ai_result.priority_tier}, score: {ai_result.lead_score}")

    # ── Stage 6: Store embedding for future lookups ────────────────────────────
    _store_lead_in_qdrant(clean_lead, message_embedding, ai_result)

    # ── Stage 7a: Write to Google Sheets ──────────────────────────────────────
    try:
        append_lead_to_sheet(
            clean_lead,
            ai_result,
            settings.google_sheet_id,
            settings.google_service_account_json,
        )
    except Exception as error:
        # Sheet failure is non-fatal — the Slack notification still fires below
        logger.error(f"Google Sheets write failed: {error}")

    # ── Stage 7b: Slack notification ──────────────────────────────────────────
    is_hot_lead = ai_result.priority_tier == "Hot"
    slack_webhook = settings.slack_webhook_url_urgent if is_hot_lead else settings.slack_webhook_url_default

    try:
        send_qualified_lead_notification(clean_lead, ai_result, slack_webhook, is_urgent=is_hot_lead)
    except Exception as error:
        logger.error(f"Slack notification failed: {error}")

    # ── Return result to caller ────────────────────────────────────────────────
    return WebhookResponse(
        status="qualified",
        priority_tier=ai_result.priority_tier,
        lead_score=ai_result.lead_score,
        intent_summary=ai_result.intent_summary,
        suggested_opener=ai_result.suggested_opener,
        red_flags=ai_result.red_flags,
        prior_context_used=ai_result.prior_context_used,
        manual_review=ai_result.manual_review,
    )


@app.get("/health")
async def health_check():
    """Simple liveness check — useful for monitoring and docker-compose readiness probes."""
    return {"status": "ok"}


# ── Private helpers ────────────────────────────────────────────────────────────
# These keep the main endpoint readable by hiding Slack alert wiring that would
# otherwise clutter the primary control flow.


def _alert_incomplete_lead(raw_email: str, reason: str) -> None:
    """Fires a Slack alert for leads that failed required-field validation."""
    try:
        send_incomplete_lead_alert(raw_email, reason, settings.slack_webhook_url_default)
    except Exception as error:
        logger.error(f"Could not send incomplete-lead Slack alert: {error}")


def _alert_low_context_lead(lead: LeadInput) -> None:
    """Fires a lightweight Slack notice for leads with a message too short to score."""
    try:
        send_low_context_alert(lead, settings.slack_webhook_url_default)
    except Exception as error:
        logger.error(f"Could not send low-context Slack alert: {error}")


def _lookup_similar_lead(message_embedding: list[float]) -> dict | None:
    """
    Queries Qdrant for a similar past lead.
    Returns None and logs a warning if Qdrant is unavailable — this is non-fatal.
    """
    try:
        return find_similar_lead(qdrant_client, message_embedding, settings.similarity_threshold)
    except Exception as error:
        logger.warning(f"Qdrant lookup failed — continuing without prior context: {error}")
        return None


def _store_lead_in_qdrant(lead: LeadInput, embedding: list[float], ai_result: AIResult) -> None:
    """
    Upserts this lead's embedding and metadata into Qdrant.
    Logs a warning and continues if the write fails — vector memory is best-effort.
    """
    lead_metadata = {
        "full_name": lead.full_name,
        "email": lead.email,
        "company_name": lead.company_name,
        "lead_score": ai_result.lead_score,
        "priority_tier": ai_result.priority_tier,
    }
    try:
        store_lead_embedding(qdrant_client, embedding, lead_metadata)
    except Exception as error:
        logger.warning(f"Qdrant embedding storage failed: {error}")
