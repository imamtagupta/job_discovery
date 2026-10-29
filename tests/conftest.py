from pathlib import Path

import pytest
import yaml


@pytest.fixture
def fixture_dir() -> Path:
    return Path(__file__).parent / "fixtures"


@pytest.fixture
def profile() -> dict:
    return yaml.safe_load(Path("config/profile.yaml").read_text())


@pytest.fixture
def scoring_config() -> dict:
    return yaml.safe_load(Path("config/scoring.yaml").read_text())
