import pytest
from pydantic import ValidationError

from job_discovery.config import CompaniesConfig
from job_discovery.database import DatabaseConfigurationError, make_engine
from job_discovery.services.sheets import safe_cell


def test_enabled_greenhouse_requires_token():
    with pytest.raises(ValidationError):
        CompaniesConfig.model_validate(
            {"companies": [{"id": "acme", "name": "Acme", "sources": [{"type": "greenhouse"}]}]}
        )


def test_formula_injection_is_escaped():
    assert safe_cell("=IMPORTXML('evil')").startswith("'")
    assert safe_cell("https://example.com") == "https://example.com"


@pytest.mark.parametrize("database_url", ["", "not-a-database-url"])
def test_invalid_database_url_has_actionable_safe_error(database_url):
    with pytest.raises(DatabaseConfigurationError) as exc:
        make_engine(database_url)

    message = str(exc.value)
    assert "DATABASE_URL" in message
    assert "postgresql+psycopg://" in message


def test_sqlite_database_url_is_accepted():
    engine = make_engine("sqlite:///:memory:")
    try:
        assert engine.dialect.name == "sqlite"
    finally:
        engine.dispose()
