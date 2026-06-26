import re
from dataclasses import dataclass

from models import LeadInput


@dataclass
class ValidationResult:
    """Carries the outcome of the validation stage through the pipeline."""

    is_valid: bool
    is_low_context: bool
    error_reason: str           # non-empty only when is_valid is False
    normalized_lead: LeadInput | None  # non-None only when is_valid is True


def validate_and_normalize(lead: LeadInput, min_message_length: int) -> ValidationResult:
    """
    Runs all three validation and normalization steps in sequence.

    Step 1 — Required fields: full_name, email, and message must all be present
              and non-empty. Returns early with an error if any are missing.
    Step 2 — Normalize: trim whitespace from all fields, lowercase the email,
              and confirm the email matches a standard format.
    Step 3 — Message length: if the message is present but very short,
              flag the lead as low-context so the caller can skip AI scoring.
    """

    # ── Step 1: Check required fields ─────────────────────────────────────────
    if not lead.full_name.strip():
        return ValidationResult(
            is_valid=False,
            is_low_context=False,
            error_reason="full_name is missing or empty",
            normalized_lead=None,
        )

    if not lead.email.strip():
        return ValidationResult(
            is_valid=False,
            is_low_context=False,
            error_reason="email is missing or empty",
            normalized_lead=None,
        )

    if not lead.message.strip():
        return ValidationResult(
            is_valid=False,
            is_low_context=False,
            error_reason="message is missing or empty",
            normalized_lead=None,
        )

    # ── Step 2: Normalize ──────────────────────────────────────────────────────
    normalized_email = lead.email.strip().lower()

    if not _is_valid_email_format(normalized_email):
        return ValidationResult(
            is_valid=False,
            is_low_context=False,
            error_reason=f"email format is invalid: {normalized_email}",
            normalized_lead=None,
        )

    normalized_lead = LeadInput(
        full_name=lead.full_name.strip(),
        email=normalized_email,
        company_name=lead.company_name.strip(),
        job_title=lead.job_title.strip(),
        phone=lead.phone.strip(),
        company_size=lead.company_size.strip(),
        budget_range=lead.budget_range.strip(),
        message=lead.message.strip(),
    )

    # ── Step 3: Branch on message length ──────────────────────────────────────
    message_length = len(normalized_lead.message)
    is_low_context = message_length < min_message_length

    return ValidationResult(
        is_valid=True,
        is_low_context=is_low_context,
        error_reason="",
        normalized_lead=normalized_lead,
    )


def _is_valid_email_format(email: str) -> bool:
    """Returns True if the string matches a standard email address pattern."""
    pattern = r"^[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}$"
    return bool(re.match(pattern, email))
