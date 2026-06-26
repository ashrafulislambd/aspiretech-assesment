import json
import logging

import groq as groq_sdk

from models import AIResult, LeadInput

logger = logging.getLogger(__name__)

GROQ_MODEL = "llama-3.3-70b-versatile"

# The system prompt stays fixed for every request.
# It tells the model its role, the scoring rules, and the exact JSON schema
# it must return — Groq's JSON mode guarantees valid JSON but we enforce the
# shape via the prompt.
SYSTEM_PROMPT = """\
You are a B2B sales qualification assistant. Evaluate inbound leads and score \
them based on fit, urgency, and buying intent.

Scoring guidelines:
- Hot  (score 70–100): Clear business pain, specific budget, decision-maker title, urgent timeline
- Warm (score 40–69):  Real business interest but missing one or more strong buying signals
- Cold (score 1–39):   Vague inquiry, student/researcher, spam, competitor, or no budget signal

If the message reads like spam, a student project, competitor research, or \
generic curiosity with no business need, score it Cold and populate red_flags accordingly.

You MUST respond with a single valid JSON object matching this exact schema — \
no extra keys, no prose outside the JSON:
{
  "lead_score": <integer 1–100>,
  "priority_tier": <"Hot" | "Warm" | "Cold">,
  "intent_summary": <one sentence describing what the lead actually needs>,
  "suggested_opener": <one personalised first sentence to open a sales reply>,
  "red_flags": <array of short strings, empty array if none>
}\
"""


def build_lead_prompt(lead: LeadInput, prior_context: dict | None) -> str:
    """
    Formats the lead's relevant fields into a clean data block for the AI prompt.

    Only the fields the model actually needs are included — not the full raw
    payload — to keep token usage lean and the prompt focused.

    If a similar past lead was found in Qdrant, a prior context block is appended
    so the model can factor in repeat or returning inquiry signals.
    """
    lead_data_block = f"""\
Lead Details:
- Name:         {lead.full_name}
- Job Title:    {lead.job_title or "not provided"}
- Company:      {lead.company_name or "not provided"}
- Company Size: {lead.company_size or "not provided"}
- Budget Range: {lead.budget_range or "not provided"}
- Message:      {lead.message}\
"""

    if prior_context is None:
        return lead_data_block

    prior_score   = prior_context["payload"].get("lead_score", "unknown")
    prior_tier    = prior_context["payload"].get("priority_tier", "unknown")
    prior_company = prior_context["payload"].get("company_name", "unknown")
    similarity    = round(prior_context["score"], 2)

    prior_context_block = f"""

Prior Context (similar past lead detected — similarity: {similarity}):
A previous lead from '{prior_company}' scored {prior_score} and was rated '{prior_tier}'.
Factor this into your analysis — this may be a returning or repeat inquiry from the same organization.\
"""

    return lead_data_block + prior_context_block


def build_fallback_ai_result() -> AIResult:
    """
    Returns a safe default AIResult when the AI call fails for any reason.

    The manual_review flag signals downstream steps (Sheets, Slack) that a
    human needs to look at this lead rather than acting on the AI score.
    """
    return AIResult(
        lead_score=0,
        priority_tier="Manual Review",
        intent_summary="AI analysis unavailable — please review this lead manually.",
        suggested_opener="Please review this lead manually before reaching out.",
        red_flags=["ai_error"],
        manual_review=True,
    )


def analyze_lead(lead: LeadInput, prior_context: dict | None, groq_api_key: str) -> AIResult:
    """
    Calls Groq (LLaMA 3.3 70B) to score and summarize the lead.

    JSON mode is enabled so the model is constrained to return valid JSON.
    The system prompt further constrains the shape to match our schema.

    Returns a fallback result (rather than raising) on any API failure so the
    pipeline continues and the lead still reaches Sheets and Slack.
    """
    # Defensive guard: the validator blocks empty messages in Step 1, but if one
    # somehow reaches here, return the fallback rather than wasting an API call.
    if not lead.message:
        logger.warning("analyze_lead called with empty message — returning fallback result.")
        return build_fallback_ai_result()

    client       = groq_sdk.Groq(api_key=groq_api_key)
    user_message = build_lead_prompt(lead, prior_context)

    try:
        response = client.chat.completions.create(
            model=GROQ_MODEL,
            messages=[
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user",   "content": user_message},
            ],
            response_format={"type": "json_object"},
            temperature=0,    # scoring task — deterministic output
        )

    except groq_sdk.RateLimitError as error:
        # 429 — hit the free-tier rate limit; return fallback so pipeline continues
        logger.error(f"Groq rate limit hit: {error} — using fallback result.")
        return build_fallback_ai_result()

    except groq_sdk.APIStatusError as error:
        # 4xx/5xx from Groq server
        logger.error(f"Groq API error: {error} — using fallback result.")
        return build_fallback_ai_result()

    except groq_sdk.APIConnectionError as error:
        # Network timeout or connection refused
        logger.error(f"Groq connection error: {error} — using fallback result.")
        return build_fallback_ai_result()

    except Exception as error:
        # Catch-all for any other unexpected failure — keep the pipeline alive
        logger.error(f"Unexpected error calling Groq: {error} — using fallback result.")
        return build_fallback_ai_result()

    try:
        raw_text = response.choices[0].message.content
        ai_data  = json.loads(raw_text)
        return AIResult(
            lead_score     =ai_data["lead_score"],
            priority_tier  =ai_data["priority_tier"],
            intent_summary =ai_data["intent_summary"],
            suggested_opener=ai_data["suggested_opener"],
            red_flags      =ai_data["red_flags"],
        )

    except (json.JSONDecodeError, KeyError, ValueError) as error:
        # Response was not valid JSON or was missing expected fields
        logger.error(f"Failed to parse Groq response: {error} — using fallback result.")
        return build_fallback_ai_result()
