import pytest
from pydantic import ValidationError

from job_discovery.config import CompaniesConfig
from job_discovery.services.sheets import safe_cell


def test_enabled_greenhouse_requires_token():
    with pytest.raises(ValidationError):
        CompaniesConfig.model_validate(
            {"companies": [{"id": "acme", "name": "Acme", "sources": [{"type": "greenhouse"}]}]}
        )


def test_formula_injection_is_escaped():
    assert safe_cell("=IMPORTXML('evil')").startswith("'")
    assert safe_cell("https://example.com") == "https://example.com"
