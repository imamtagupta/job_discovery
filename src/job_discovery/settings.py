from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    database_url: str = "sqlite:///job_discovery.db"
    companies_config: Path = Path("config/companies.yaml")
    target_companies_config: Path = Path("config/target_companies.yaml")
    pending_sources_config: Path = Path("config/pending_sources.yaml")
    source_health_config: Path = Path("config/source_health.yaml")
    profile_config: Path = Path("config/profile.yaml")
    scoring_config: Path = Path("config/scoring.yaml")
    log_level: str = "INFO"
    http_timeout_seconds: float = 20
    http_per_host_concurrency: int = 3
    google_sheets_spreadsheet_id: str | None = None
    google_service_account_json: str | None = None
    new_match_threshold: float = 80
