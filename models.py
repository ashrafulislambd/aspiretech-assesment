from pydantic import BaseModel


class LeadInput(BaseModel):
    """
    The raw payload arriving from the website contact form.
    All fields default to empty string so that missing fields are caught
    by our own validation logic rather than rejected immediately by Pydantic.
    """

    full_name: str = ""
    email: str = ""
    company_name: str = ""
    job_title: str = ""
    phone: str = ""
    company_size: str = ""
    budget_range: str = ""
    message: str = ""


class AIResult(BaseModel):
    """
    The structured output returned by Claude after analyzing a lead.
    All fields have safe defaults so the model can represent a fallback result
    when the AI call fails.
    """

    lead_score: int = 0
    priority_tier: str = "Manual Review"   # one of: "Hot", "Warm", "Cold", "Manual Review"
    intent_summary: str = ""
    suggested_opener: str = ""
    red_flags: list[str] = []
    manual_review: bool = False            # True when the AI call failed and a human must step in
    prior_context_used: bool = False       # True when a similar past lead was found in Qdrant


class WebhookResponse(BaseModel):
    """
    The JSON object returned to the original caller after the pipeline completes.
    Useful for a live website widget or chatbot that wants an immediate answer.
    """

    status: str                     # "qualified", "low_context", or "invalid"
    priority_tier: str
    lead_score: int
    intent_summary: str
    suggested_opener: str
    red_flags: list[str] = []       # passed through so the frontend can display them
    prior_context_used: bool = False # True when Qdrant found a similar past lead
    manual_review: bool = False      # True when AI call failed and a human must step in
