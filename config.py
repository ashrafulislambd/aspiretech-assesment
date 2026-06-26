from functools import lru_cache
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """
    All application settings loaded from environment variables.
    Defaults are provided where the value is safe to share (non-secret).
    """

    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8")

    # AI provider
    groq_api_key: str

    # Google Sheets
    google_service_account_json: str = "/app/google_credentials.json"
    google_sheet_id: str

    # Slack
    slack_webhook_url_default: str  # used for Warm and Cold leads
    slack_webhook_url_urgent: str   # used for Hot leads

    # Qdrant vector database
    qdrant_host: str = "qdrant"
    qdrant_port: int = 6333

    # Pipeline thresholds
    min_message_length: int = 10       # messages shorter than this skip AI scoring
    similarity_threshold: float = 0.82 # cosine similarity required to count as a matching past lead


@lru_cache
def get_settings() -> Settings:
    """Returns the cached Settings instance (reads .env once per process)."""
    return Settings()
