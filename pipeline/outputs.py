import logging

import gspread
import httpx
from google.oauth2.service_account import Credentials

from models import AIResult, LeadInput

logger = logging.getLogger(__name__)

GOOGLE_SCOPES = [
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
]

# Column order matches the headers that must exist in row 1 of your Google Sheet
SHEET_COLUMN_ORDER = [
    "full_name",
    "email",
    "company_name",
    "job_title",
    "company_size",
    "budget_range",
    "message",
    "lead_score",
    "priority_tier",
    "intent_summary",
    "suggested_opener",
    "red_flags",
    "manual_review",
    "prior_context_used",
]

# Emoji prefix per priority tier — makes the Slack message scannable at a glance
TIER_EMOJI = {
    "Hot": "🔥",
    "Warm": "⚡",
    "Cold": "❄️",
    "Manual Review": "⚠️",
}


# ── Google Sheets ──────────────────────────────────────────────────────────────


def append_lead_to_sheet(
    lead: LeadInput,
    ai_result: AIResult,
    sheet_id: str,
    credentials_path: str,
) -> None:
    """Appends one enriched lead record as a new row at the bottom of the Google Sheet."""
    credentials = Credentials.from_service_account_file(credentials_path, scopes=GOOGLE_SCOPES)
    sheets_client = gspread.authorize(credentials)
    sheet = sheets_client.open_by_key(sheet_id).sheet1

    # Build a dict keyed by column name, then use SHEET_COLUMN_ORDER to produce
    # the row list. Adding or removing a column only requires changing one place.
    field_values: dict[str, str | int] = {
        "full_name": lead.full_name,
        "email": lead.email,
        "company_name": lead.company_name,
        "job_title": lead.job_title,
        "company_size": lead.company_size,
        "budget_range": lead.budget_range,
        "message": lead.message,
        "lead_score": ai_result.lead_score,
        "priority_tier": ai_result.priority_tier,
        "intent_summary": ai_result.intent_summary,
        "suggested_opener": ai_result.suggested_opener,
        "red_flags": ", ".join(ai_result.red_flags),
        "manual_review": str(ai_result.manual_review),
        "prior_context_used": str(ai_result.prior_context_used),
    }

    row_values = [field_values[column_name] for column_name in SHEET_COLUMN_ORDER]

    sheet.append_row(row_values, value_input_option="USER_ENTERED")
    logger.info(f"Appended row to Google Sheet for: {lead.email}")


# ── Slack Notifications ────────────────────────────────────────────────────────


def send_qualified_lead_notification(
    lead: LeadInput,
    ai_result: AIResult,
    webhook_url: str,
    is_urgent: bool,
) -> None:
    """
    Sends a rich Slack notification for a lead that has been scored by the AI.

    Hot leads include an @here mention so the sales team is alerted immediately.
    Warm and Cold leads use the same format but without the mention.
    """
    emoji = TIER_EMOJI.get(ai_result.priority_tier, "📋")

    # The @here mention only goes out for Hot leads routed to the urgent channel
    urgency_line = "<!here> *New hot lead — act now.*\n\n" if is_urgent else ""

    red_flags_line = ""
    if ai_result.red_flags:
        red_flags_text = ", ".join(ai_result.red_flags)
        red_flags_line = f"\n*Red Flags:* {red_flags_text}"

    prior_context_line = ""
    if ai_result.prior_context_used:
        prior_context_line = "\n_ℹ️ Similar past lead found — prior context was factored in._"

    message_body = (
        f"{urgency_line}"
        f"{emoji} *{ai_result.priority_tier} Lead — Score: {ai_result.lead_score}/100*\n\n"
        f"*Name:* {lead.full_name}  |  *Company:* {lead.company_name or 'N/A'}\n"
        f"*Title:* {lead.job_title or 'N/A'}  |  *Budget:* {lead.budget_range or 'N/A'}\n\n"
        f"*Intent:* {ai_result.intent_summary}\n\n"
        f"*Suggested Opener:*\n>{ai_result.suggested_opener}"
        f"{red_flags_line}"
        f"{prior_context_line}"
    )

    slack_payload = {
        "text": f"{emoji} {ai_result.priority_tier} Lead: {lead.full_name} @ {lead.company_name}",
        "blocks": [
            {
                "type": "section",
                "text": {"type": "mrkdwn", "text": message_body},
            }
        ],
    }

    _post_to_slack(webhook_url, slack_payload)
    logger.info(f"Slack notification sent for qualified lead: {lead.email}")


def send_incomplete_lead_alert(raw_email: str, reason: str, webhook_url: str) -> None:
    """Sends a brief Slack alert when a lead submission fails required-field validation."""
    email_display = raw_email if raw_email else "unknown"

    slack_payload = {
        "text": f"⚠️ Incomplete lead from {email_display}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"⚠️ *Incomplete Lead Submission*\n"
                        f"*Email:* {email_display}\n"
                        f"*Reason:* {reason}"
                    ),
                },
            }
        ],
    }

    _post_to_slack(webhook_url, slack_payload)
    logger.info(f"Slack incomplete-lead alert sent for: {email_display}")


def send_low_context_alert(lead: LeadInput, webhook_url: str) -> None:
    """Sends a lightweight Slack notice for a lead whose message is too short to qualify."""
    slack_payload = {
        "text": f"❄️ Low-context lead from {lead.full_name} @ {lead.company_name or 'unknown'}",
        "blocks": [
            {
                "type": "section",
                "text": {
                    "type": "mrkdwn",
                    "text": (
                        f"❄️ *Low-Context Lead — No AI Scoring*\n"
                        f"*Name:* {lead.full_name}  |  *Email:* {lead.email}\n"
                        f"*Company:* {lead.company_name or 'N/A'}\n"
                        f"*Message:* \"{lead.message}\"\n"
                        f"_Message too short to qualify automatically. Follow up if this looks relevant._"
                    ),
                },
            }
        ],
    }

    _post_to_slack(webhook_url, slack_payload)
    logger.info(f"Slack low-context alert sent for: {lead.email}")


def _post_to_slack(webhook_url: str, payload: dict) -> None:
    """POSTs a payload to a Slack Incoming Webhook URL and raises on HTTP error."""
    response = httpx.post(webhook_url, json=payload, timeout=10)
    response.raise_for_status()
