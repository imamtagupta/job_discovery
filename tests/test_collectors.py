import json

import httpx

from job_discovery.collectors.base import AsyncHttpClient
from job_discovery.collectors.greenhouse import GreenhouseCollector
from job_discovery.collectors.lever import LeverCollector
from job_discovery.config import CompanyConfig, SourceConfig


async def test_greenhouse_normalization(fixture_dir):
    payload = json.loads((fixture_dir / "greenhouse.json").read_text())
    transport = httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    http = AsyncHttpClient(transport=transport)
    try:
        company = CompanyConfig(id="acme", name="Acme", sources=[])
        jobs = await GreenhouseCollector(http).collect(
            company, SourceConfig(type="greenhouse", board_token="acme")
        )
        assert jobs[0].external_job_id == "101"
        assert jobs[0].description_text == "Build APIs with Python and FastAPI."
        assert jobs[0].location == "Mumbai, India"
    finally:
        await http.close()


async def test_lever_normalization_and_region(fixture_dir):
    payload = json.loads((fixture_dir / "lever.json").read_text())
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(200, json=payload)

    http = AsyncHttpClient(transport=httpx.MockTransport(handler))
    try:
        company = CompanyConfig(id="acme", name="Acme", sources=[])
        jobs = await LeverCollector(http).collect(
            company, SourceConfig(type="lever", site="acme", region="eu")
        )
        assert seen[0].startswith("https://api.eu.lever.co/")
        assert jobs[0].employment_type == "Full-time"
        assert "PostgreSQL" in jobs[0].description_text
    finally:
        await http.close()
