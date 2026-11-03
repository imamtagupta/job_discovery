import json

import httpx
import yaml

from discovery.ats_detector import (
    AtsDetector,
    CandidateSource,
    discover_registry,
    extract_candidates,
    merge_verified_source,
)
from job_discovery.collectors.base import AsyncHttpClient


def test_target_registry_has_150_unique_candidates():
    document = yaml.safe_load(open("config/target_companies.yaml", encoding="utf-8"))
    companies = document["companies"]
    assert len(companies) == 150
    assert len({company["id"] for company in companies}) == 150
    assert all(company["careers_url"].startswith("https://") for company in companies)


def test_extracts_supported_and_unsupported_urls():
    html = """
    <a href="https://boards.greenhouse.io/acme">Jobs</a>
    <script src="https://jobs.ashbyhq.com/acme"></script>
    <a href="https://acme.wd5.myworkdayjobs.com/en-US/External">Workday</a>
    <a href="https://jobs.smartrecruiters.com/Acme">Other</a>
    """
    candidates, unsupported = extract_candidates(
        "acme", "Acme", html, "https://acme.example/careers"
    )
    providers = {candidate.provider for candidate in candidates}
    assert providers == {"greenhouse", "ashby", "workday", "smartrecruiters"}
    workday = next(item for item in candidates if item.provider == "workday")
    assert workday.identifiers == {
        "tenant": "acme",
        "workday_cluster": "wd5",
        "site_id": "External",
    }
    assert unsupported == []


async def test_provider_schema_validation():
    def handler(request: httpx.Request) -> httpx.Response:
        if "greenhouse" in request.url.host:
            return httpx.Response(200, json={"jobs": [{"id": 1}]})
        if "lever" in request.url.host:
            return httpx.Response(200, json=[{"id": "1", "text": "Engineer"}])
        if "ashby" in request.url.host:
            return httpx.Response(200, json={"jobs": []})
        return httpx.Response(200, json={"jobPostings": [], "total": 0})

    http = AsyncHttpClient(transport=httpx.MockTransport(handler))
    detector = AtsDetector(http)
    candidates = [
        CandidateSource("acme", "Acme", "greenhouse", {"board_token": "acme"}, "x"),
        CandidateSource("acme", "Acme", "lever", {"site": "acme", "region": "global"}, "x"),
        CandidateSource("acme", "Acme", "ashby", {"board_name": "acme"}, "x"),
        CandidateSource(
            "acme",
            "Acme",
            "workday",
            {"tenant": "acme", "workday_cluster": "wd5", "site_id": "External"},
            "x",
        ),
    ]
    try:
        results = [await detector.validate(candidate) for candidate in candidates]
    finally:
        await http.close()
    assert [result.status for result in results] == ["verified"] * 4


def test_manual_verified_identifier_is_not_overwritten():
    companies = [
        {
            "id": "acme",
            "name": "Acme",
            "sources": [
                {
                    "type": "greenhouse",
                    "board_token": "manual-token",
                    "enabled": True,
                    "verification": {"status": "verified"},
                }
            ],
        }
    ]
    candidate = CandidateSource(
        "acme", "Acme", "greenhouse", {"board_token": "different-token"}, "evidence"
    )
    from discovery.ats_detector import ValidationResult

    changed, reason = merge_verified_source(
        companies, ValidationResult(candidate, "verified", "validation", job_count=1)
    )
    assert not changed
    assert "conflicts" in reason
    assert companies[0]["sources"][0]["board_token"] == "manual-token"


async def test_discovery_writes_only_validated_sources(tmp_path):
    targets = tmp_path / "targets.yaml"
    companies = tmp_path / "companies.yaml"
    pending = tmp_path / "pending.yaml"
    health = tmp_path / "health.yaml"
    targets.write_text(
        yaml.safe_dump(
            {
                "companies": [
                    {
                        "id": "acme",
                        "name": "Acme",
                        "careers_url": "https://acme.example/careers",
                    },
                    {
                        "id": "broken",
                        "name": "Broken",
                        "careers_url": "https://broken.example/careers",
                    },
                ]
            }
        )
    )
    companies.write_text("companies: []\n")
    pending.write_text("pending_sources: []\n")

    def handler(request: httpx.Request) -> httpx.Response:
        url = str(request.url)
        if url == "https://acme.example/careers":
            return httpx.Response(
                200,
                text='<a href="https://boards.greenhouse.io/acme">Openings</a>',
            )
        if "boards-api.greenhouse.io" in url:
            return httpx.Response(200, json={"jobs": []})
        if url == "https://broken.example/careers":
            return httpx.Response(
                200,
                text='<a href="https://jobs.lever.co/wrong">Openings</a>',
            )
        return httpx.Response(200, content=json.dumps({"unexpected": True}).encode())

    http = AsyncHttpClient(transport=httpx.MockTransport(handler))
    try:
        summary = await discover_registry(targets, companies, pending, health, http=http)
    finally:
        await http.close()
    saved = yaml.safe_load(companies.read_text())["companies"]
    uncertain = yaml.safe_load(pending.read_text())["pending_sources"]
    assert summary["total_companies_researched"] == 2
    assert summary["verified_ats_sources"] == 1
    assert saved[0]["sources"][0]["board_token"] == "acme"
    assert saved[0]["sources"][0]["enabled"] is True
    assert uncertain[0]["verification_status"] == "failed_validation"
