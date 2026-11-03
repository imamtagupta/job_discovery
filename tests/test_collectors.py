import json

import httpx

from job_discovery.collectors.amazon import AmazonCollector
from job_discovery.collectors.base import AsyncHttpClient
from job_discovery.collectors.greenhouse import GreenhouseCollector
from job_discovery.collectors.icims import IcimsCollector
from job_discovery.collectors.lever import LeverCollector
from job_discovery.collectors.oracle import OracleRecruitingCollector
from job_discovery.collectors.smartrecruiters import SmartRecruitersCollector
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


async def test_amazon_normalization():
    payload = {
        "hits": 1,
        "jobs": [
            {
                "id": "uuid",
                "id_icims": "123",
                "title": "Software Engineer",
                "job_path": "/en/jobs/123/software-engineer",
                "location": "IN, KA, Bangalore",
                "description": "<p>Build Python services.</p>",
                "posted_date": "2026-11-01",
            }
        ],
    }
    http = AsyncHttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    try:
        company = CompanyConfig(id="amazon", name="Amazon", sources=[])
        jobs = await AmazonCollector(http).collect(company, SourceConfig(type="amazon"))
        assert jobs[0].external_job_id == "123"
        assert jobs[0].description_text == "Build Python services."
    finally:
        await http.close()


async def test_smartrecruiters_normalization():
    listing = {"totalFound": 1, "content": [{"id": "abc"}]}
    detail = {
        "id": "abc",
        "name": "Backend Engineer",
        "refNumber": "REF-1",
        "location": {"fullLocation": "Bengaluru, India"},
        "jobAd": {"sections": {"jobDescription": {"text": "<p>Python APIs</p>"}}},
        "applyUrl": "https://jobs.smartrecruiters.com/acme/abc",
    }

    def handler(request):
        return httpx.Response(200, json=detail if str(request.url).endswith("/abc") else listing)

    http = AsyncHttpClient(transport=httpx.MockTransport(handler))
    try:
        company = CompanyConfig(id="acme", name="Acme", sources=[])
        source = SourceConfig(type="smartrecruiters", company_identifier="acme")
        jobs = await SmartRecruitersCollector(http).collect(company, source)
        assert jobs[0].requisition_id == "REF-1"
        assert jobs[0].location == "Bengaluru, India"
    finally:
        await http.close()


async def test_oracle_recruiting_normalization():
    payload = {
        "items": [
            {
                "Id": 42,
                "RequisitionNumber": "REQ-42",
                "Title": "Senior Backend Engineer",
                "PrimaryLocation": "Bengaluru, India",
                "ExternalDescriptionStr": "<p>Build distributed Python services.</p>",
                "ExternalPostedStartDate": "2026-11-02T00:00:00Z",
            }
        ],
        "hasMore": False,
    }
    http = AsyncHttpClient(
        transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))
    )
    try:
        company = CompanyConfig(id="acme", name="Acme", sources=[])
        source = SourceConfig(
            type="oracle-recruiting",
            oracle_host="https://acme.fa.oraclecloud.com",
            site_number="CX_1",
        )
        jobs = await OracleRecruitingCollector(http).collect(company, source)
        assert jobs[0].external_job_id == "42"
        assert jobs[0].requisition_id == "REQ-42"
        assert "Python services" in jobs[0].description_text
    finally:
        await http.close()


async def test_icims_normalization():
    listing = '<a href="/jobs/123/backend-engineer/job">Backend Engineer</a>'
    detail = """
    <script type="application/ld+json">
    {
      "@type": "JobPosting",
      "title": "Backend Engineer",
      "description": "<p>Python and PostgreSQL</p>",
      "datePosted": "2026-11-01",
      "identifier": {"value": "REQ-123"},
      "jobLocation": {"address": {"addressLocality": "Pune", "addressCountry": "India"}}
    }
    </script>
    """

    def handler(request):
        return httpx.Response(200, text=detail if "/jobs/123/" in request.url.path else listing)

    http = AsyncHttpClient(transport=httpx.MockTransport(handler))
    try:
        company = CompanyConfig(id="acme", name="Acme", sources=[])
        source = SourceConfig(type="icims", career_url="https://careers-acme.icims.com")
        jobs = await IcimsCollector(http).collect(company, source)
        assert jobs[0].external_job_id == "REQ-123"
        assert jobs[0].location == "Pune, India"
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
