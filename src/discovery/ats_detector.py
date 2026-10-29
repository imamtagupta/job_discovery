import asyncio
import logging
import re
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import yaml
from bs4 import BeautifulSoup

from job_discovery.collectors.base import AsyncHttpClient

logger = logging.getLogger(__name__)

ATS_PATTERNS = {
    "greenhouse": re.compile(
        r"https?://(?:boards|job-boards)\.greenhouse\.io/([a-zA-Z0-9_-]+)", re.I
    ),
    "lever": re.compile(r"https?://jobs(?:\.eu)?\.lever\.co/([a-zA-Z0-9_-]+)", re.I),
    "ashby": re.compile(r"https?://jobs\.ashbyhq\.com/([a-zA-Z0-9_-]+)", re.I),
    "workday": re.compile(
        r"https?://([a-zA-Z0-9_-]+)\.(wd\d+)\.myworkdayjobs\.com/(?:[a-z]{2}-[A-Z]{2}/)?([a-zA-Z0-9_-]+)",
        re.I,
    ),
}
UNSUPPORTED_HOSTS = {
    "smartrecruiters.com": "smartrecruiters",
    "icims.com": "icims",
    "successfactors.com": "successfactors",
    "oraclecloud.com": "oracle-recruiting",
    "bamboohr.com": "bamboohr",
    "workable.com": "workable",
}


def now_iso() -> str:
    return datetime.now(UTC).isoformat()


def normalized_name(value: str) -> str:
    return re.sub(r"[^a-z0-9]", "", value.lower())


def pending_key(record: dict[str, Any]) -> tuple[str | None, str | None, tuple]:
    identifiers = record.get("identifiers") or {}
    return (
        record.get("company_id"),
        record.get("provider") or record.get("type"),
        tuple(sorted(identifiers.items())),
    )


@dataclass(frozen=True)
class CandidateSource:
    company_id: str
    company_name: str
    provider: str
    identifiers: dict[str, str]
    evidence_url: str
    discovered_on: str = field(default_factory=now_iso)

    @property
    def key(self) -> tuple[str, str, tuple[tuple[str, str], ...]]:
        return self.company_id, self.provider, tuple(sorted(self.identifiers.items()))


@dataclass
class ValidationResult:
    candidate: CandidateSource
    status: str
    validation_url: str | None = None
    reason: str | None = None
    job_count: int | None = None
    identity_check: str = "official_page_link"

    def as_pending(self) -> dict[str, Any]:
        return {
            "company_id": self.candidate.company_id,
            "company_name": self.candidate.company_name,
            "provider": self.candidate.provider,
            "identifiers": self.candidate.identifiers,
            "evidence_url": self.candidate.evidence_url,
            "validation_url": self.validation_url,
            "verification_status": self.status,
            "reason": self.reason,
            "identity_check": self.identity_check,
            "checked_at": now_iso(),
        }


def extract_candidates(
    company_id: str, company_name: str, html: str, page_url: str
) -> tuple[list[CandidateSource], list[dict[str, str]]]:
    soup = BeautifulSoup(html, "html.parser")
    urls = {page_url}
    for element in soup.find_all(["a", "script", "iframe", "form"]):
        value = element.get("href") or element.get("src") or element.get("action")
        if value:
            urls.add(urljoin(page_url, value))
    decoded = unquote(html.replace("\\/", "/"))
    urls.update(re.findall(r"https?://[^\s\"'<>]+", decoded))

    candidates: dict[tuple, CandidateSource] = {}
    unsupported: dict[str, dict[str, str]] = {}
    for url in urls:
        for provider, pattern in ATS_PATTERNS.items():
            match = pattern.search(url)
            if not match:
                continue
            if provider == "greenhouse":
                identifiers = {"board_token": match.group(1)}
            elif provider == "lever":
                identifiers = {
                    "site": match.group(1),
                    "region": "eu" if ".eu.lever.co" in url.lower() else "global",
                }
            elif provider == "ashby":
                identifiers = {"board_name": match.group(1)}
            else:
                identifiers = {
                    "tenant": match.group(1),
                    "workday_cluster": match.group(2).lower(),
                    "site_id": match.group(3),
                }
            candidate = CandidateSource(company_id, company_name, provider, identifiers, url)
            candidates[candidate.key] = candidate
        hostname = (urlparse(url).hostname or "").lower()
        for suffix, provider in UNSUPPORTED_HOSTS.items():
            if hostname == suffix or hostname.endswith(f".{suffix}"):
                unsupported.setdefault(
                    provider,
                    {
                        "provider": provider,
                        "evidence_url": url,
                        "reason": "provider detected but adapter/validator is not supported",
                    },
                )
    return list(candidates.values()), list(unsupported.values())


class AtsDetector:
    def __init__(self, http: AsyncHttpClient):
        self.http = http

    async def discover_company(
        self, company: dict[str, Any]
    ) -> tuple[list[CandidateSource], list[dict[str, str]], dict[str, Any]]:
        started = now_iso()
        url = company["careers_url"]
        try:
            response = await self.http.request("GET", url)
            candidates, unsupported = extract_candidates(
                company["id"], company["name"], response.text, str(response.url)
            )
            status = (
                "candidates_found"
                if candidates
                else ("unsupported" if unsupported else "no_ats_detected")
            )
            return (
                candidates,
                unsupported,
                {
                    "company_id": company["id"],
                    "careers_url": url,
                    "resolved_url": str(response.url),
                    "status": status,
                    "http_status": response.status_code,
                    "candidate_count": len(candidates),
                    "checked_at": started,
                },
            )
        except Exception as exc:
            logger.warning("source_discovery_failed company=%s error=%s", company["id"], exc)
            return (
                [],
                [],
                {
                    "company_id": company["id"],
                    "careers_url": url,
                    "status": "failed",
                    "error": f"{type(exc).__name__}: {exc}",
                    "checked_at": started,
                },
            )

    async def validate(self, candidate: CandidateSource) -> ValidationResult:
        try:
            if candidate.provider == "greenhouse":
                token = candidate.identifiers["board_token"]
                url = f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs"
                payload = await self.http.get_json(url)
                valid = isinstance(payload, dict) and isinstance(payload.get("jobs"), list)
                count = len(payload.get("jobs", [])) if valid else None
            elif candidate.provider == "lever":
                region = candidate.identifiers.get("region", "global")
                host = "api.eu.lever.co" if region == "eu" else "api.lever.co"
                url = f"https://{host}/v0/postings/{candidate.identifiers['site']}"
                payload = await self.http.get_json(url, params={"mode": "json"})
                valid = isinstance(payload, list) and all(
                    isinstance(job, dict) and "id" in job and "text" in job for job in payload
                )
                count = len(payload) if valid else None
            elif candidate.provider == "ashby":
                board = candidate.identifiers["board_name"]
                url = f"https://api.ashbyhq.com/posting-api/job-board/{board}"
                payload = await self.http.get_json(url)
                valid = isinstance(payload, dict) and isinstance(payload.get("jobs"), list)
                count = len(payload.get("jobs", [])) if valid else None
            elif candidate.provider == "workday":
                ids = candidate.identifiers
                host = f"{ids['tenant']}.{ids['workday_cluster']}.myworkdayjobs.com"
                url = f"https://{host}/wday/cxs/{ids['tenant']}/{ids['site_id']}/jobs"
                payload = await self.http.post_json(
                    url, json={"appliedFacets": {}, "limit": 20, "offset": 0, "searchText": ""}
                )
                valid = (
                    isinstance(payload, dict)
                    and isinstance(payload.get("jobPostings"), list)
                    and isinstance(payload.get("total"), int)
                )
                count = payload.get("total") if valid else None
            else:
                return ValidationResult(candidate, "unsupported", reason="no validator")
        except Exception as exc:
            return ValidationResult(
                candidate,
                "failed_validation",
                validation_url=locals().get("url"),
                reason=f"{type(exc).__name__}: {exc}",
            )
        if not valid:
            return ValidationResult(
                candidate,
                "failed_validation",
                validation_url=url,
                reason="response did not match expected public job-listing schema",
            )
        return ValidationResult(candidate, "verified", validation_url=url, job_count=count)


def load_document(path: Path, root: str) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    with path.open(encoding="utf-8") as stream:
        return (yaml.safe_load(stream) or {}).get(root, [])


def write_document(path: Path, root: str, values: list[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + ".tmp")
    with temporary.open("w", encoding="utf-8") as stream:
        yaml.safe_dump({root: values}, stream, sort_keys=False, allow_unicode=True)
    temporary.replace(path)


def source_config(result: ValidationResult) -> dict[str, Any]:
    source = {"type": result.candidate.provider, **result.candidate.identifiers, "enabled": True}
    source["verification"] = {
        "status": "verified",
        "evidence_url": result.candidate.evidence_url,
        "validation_url": result.validation_url,
        "validated_at": now_iso(),
        "job_count_at_validation": result.job_count,
    }
    return source


def merge_verified_source(
    companies: list[dict[str, Any]], result: ValidationResult
) -> tuple[bool, str | None]:
    incoming = source_config(result)
    company = next(
        (item for item in companies if item.get("id") == result.candidate.company_id), None
    )
    if company is None:
        company = {
            "id": result.candidate.company_id,
            "name": result.candidate.company_name,
            "product_company": True,
            "enabled": True,
            "sources": [],
        }
        companies.append(company)
    sources = company.setdefault("sources", [])
    exact = next(
        (
            item
            for item in sources
            if item.get("type") == incoming["type"]
            and all(item.get(key) == value for key, value in result.candidate.identifiers.items())
        ),
        None,
    )
    if exact:
        if exact.get("verification", {}).get("status") == "verified":
            return False, "already verified; preserved existing entry"
        exact.update(incoming)
        return True, None
    provider_entries = [item for item in sources if item.get("type") == incoming["type"]]
    if provider_entries:
        return False, "conflicts with an existing provider identifier; manual review required"
    sources.append(incoming)
    company["enabled"] = True
    return True, None


async def discover_registry(
    targets_path: Path,
    companies_path: Path,
    pending_path: Path,
    health_path: Path,
    *,
    timeout: float = 20,
    per_host: int = 3,
    http: AsyncHttpClient | None = None,
) -> dict[str, int]:
    targets = load_document(targets_path, "companies")
    companies = load_document(companies_path, "companies")
    existing_pending = load_document(pending_path, "pending_sources")
    own_http = http is None
    http = http or AsyncHttpClient(timeout=timeout, per_host=per_host)
    detector = AtsDetector(http)
    health: list[dict[str, Any]] = []
    pending_by_key = {pending_key(item): item for item in existing_pending}
    summary = {
        "total_companies_researched": len(targets),
        "verified_ats_sources": 0,
        "pending_verification": 0,
        "unsupported_sources": 0,
        "failed_validation": 0,
    }
    try:
        discoveries = await asyncio.gather(
            *(detector.discover_company(target) for target in targets)
        )
        for (candidates, unsupported, company_health), target in zip(
            discoveries, targets, strict=True
        ):
            health.append(company_health)
            for item in unsupported:
                pending = {
                    "company_id": target["id"],
                    "company_name": target["name"],
                    **item,
                    "verification_status": "unsupported",
                    "checked_at": now_iso(),
                }
                pending_by_key[pending_key(pending)] = pending
            results = await asyncio.gather(*(detector.validate(item) for item in candidates))
            for result in results:
                if result.status == "verified":
                    changed, reason = merge_verified_source(companies, result)
                    pending_by_key.pop(result.candidate.key, None)
                    if changed or reason == "already verified; preserved existing entry":
                        summary["verified_ats_sources"] += 1
                    if reason and "conflicts" in reason:
                        result.status = "pending_verification"
                        result.reason = reason
                        pending_by_key[result.candidate.key] = result.as_pending()
                else:
                    summary["failed_validation"] += result.status == "failed_validation"
                    pending_by_key[result.candidate.key] = result.as_pending()
        statuses = [item.get("verification_status") for item in pending_by_key.values()]
        summary["pending_verification"] = statuses.count("pending_verification")
        summary["unsupported_sources"] = statuses.count("unsupported")
        summary["failed_validation"] = statuses.count("failed_validation")
        write_document(companies_path, "companies", companies)
        write_document(pending_path, "pending_sources", list(pending_by_key.values()))
        write_document(health_path, "companies", health)
        return summary
    finally:
        if own_http:
            await http.close()


def candidate_from_record(record: dict[str, Any]) -> CandidateSource | None:
    if record.get("enabled") is False:
        return None
    provider = record.get("provider") or record.get("type")
    identifiers = record.get("identifiers")
    if identifiers is None and provider:
        names = {
            "greenhouse": ("board_token",),
            "lever": ("site", "region"),
            "ashby": ("board_name",),
            "workday": ("tenant", "workday_cluster", "site_id"),
        }.get(provider, ())
        identifiers = {name: record[name] for name in names if record.get(name) is not None}
    if provider not in ATS_PATTERNS or not identifiers:
        return None
    if any(str(value).startswith("replace-with-") for value in identifiers.values()):
        return None
    return CandidateSource(
        company_id=record["company_id"],
        company_name=record.get("company_name", record["company_id"]),
        provider=provider,
        identifiers=identifiers,
        evidence_url=record.get("evidence_url", "manual_configuration"),
    )


async def validate_registry(
    companies_path: Path,
    pending_path: Path,
    *,
    timeout: float = 20,
    per_host: int = 3,
    http: AsyncHttpClient | None = None,
) -> dict[str, int]:
    companies = load_document(companies_path, "companies")
    pending = [
        item
        for item in load_document(pending_path, "pending_sources")
        if candidate_from_record(item) is not None
        or item.get("verification_status") == "unsupported"
    ]
    own_http = http is None
    http = http or AsyncHttpClient(timeout=timeout, per_host=per_host)
    detector = AtsDetector(http)
    candidates: list[CandidateSource] = []
    for company in companies:
        for source in company.get("sources", []):
            record = {
                **source,
                "company_id": company["id"],
                "company_name": company["name"],
                "evidence_url": source.get("verification", {}).get("evidence_url"),
            }
            if candidate := candidate_from_record(record):
                candidates.append(candidate)
    candidates.extend(
        candidate for record in pending if (candidate := candidate_from_record(record)) is not None
    )
    unique = {candidate.key: candidate for candidate in candidates}
    summary = {
        "total_companies_researched": len({item.company_id for item in unique.values()}),
        "verified_ats_sources": 0,
        "pending_verification": 0,
        "unsupported_sources": sum(
            item.get("verification_status") == "unsupported" for item in pending
        ),
        "failed_validation": 0,
    }
    remaining = {
        pending_key(item): item
        for item in pending
        if item.get("verification_status") == "unsupported"
    }
    try:
        results = await asyncio.gather(
            *(detector.validate(candidate) for candidate in unique.values())
        )
        for result in results:
            if result.status == "verified":
                changed, reason = merge_verified_source(companies, result)
                if changed or reason == "already verified; preserved existing entry":
                    summary["verified_ats_sources"] += 1
                if reason and "conflicts" in reason:
                    result.status = "pending_verification"
                    result.reason = reason
                    record = result.as_pending()
                    remaining[pending_key(record)] = record
            else:
                record = result.as_pending()
                remaining[pending_key(record)] = record
        records = list(remaining.values())
        statuses = [item.get("verification_status") for item in records]
        summary["pending_verification"] = statuses.count("pending_verification")
        summary["unsupported_sources"] = statuses.count("unsupported")
        summary["failed_validation"] = statuses.count("failed_validation")
        write_document(companies_path, "companies", companies)
        write_document(pending_path, "pending_sources", records)
        return summary
    finally:
        if own_http:
            await http.close()


def registry_summary(
    targets_path: Path, companies_path: Path, pending_path: Path, health_path: Path
) -> dict[str, int]:
    del targets_path  # Kept in the public signature for CLI/config symmetry.
    companies = load_document(companies_path, "companies")
    pending = load_document(pending_path, "pending_sources")
    health = load_document(health_path, "companies")
    verified = sum(
        source.get("enabled", False) and source.get("verification", {}).get("status") == "verified"
        for company in companies
        for source in company.get("sources", [])
    )
    return {
        "total_companies_researched": len(health),
        "verified_ats_sources": verified,
        "pending_verification": sum(
            item.get("verification_status") not in {"unsupported", "failed_validation"}
            for item in pending
        ),
        "unsupported_sources": sum(
            item.get("verification_status") == "unsupported" for item in pending
        ),
        "failed_validation": sum(
            item.get("verification_status") == "failed_validation" for item in pending
        ),
    }
