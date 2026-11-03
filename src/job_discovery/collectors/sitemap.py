import asyncio
import re
from xml.etree import ElementTree

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector
from .helpers import job_posting_jsonld, jsonld_location, parse_date, text_from_html


class SitemapCollector(Collector):
    """First-party collector for official sites publishing JobPosting JSON-LD in a sitemap."""

    source_type = "sitemap"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        response = await self.http.request("GET", source.sitemap_url)
        root = ElementTree.fromstring(response.content)
        pattern = re.compile(source.job_url_pattern)
        links = [
            node.text
            for node in root.findall(".//{*}loc")
            if node.text and pattern.search(node.text)
        ]
        if not links:
            raise ValueError("official sitemap contained no matching job URLs")

        async def fetch(url: str):
            return url, await self.http.request("GET", url)

        jobs = []
        for start in range(0, len(links), 50):
            results = await asyncio.gather(*(fetch(url) for url in links[start : start + 50]))
            for url, detail in results:
                payload = job_posting_jsonld(detail.text)
                if not payload:
                    continue
                location = jsonld_location(payload)
                if (
                    source.location_filter
                    and source.location_filter.casefold() not in (location or "").casefold()
                ):
                    continue
                identifier = payload.get("identifier", {})
                job_id = identifier.get("value") if isinstance(identifier, dict) else identifier
                if not job_id:
                    match = re.search(r"/(\d+)(?:[-/]|$)", url)
                    job_id = match.group(1) if match else url
                html = payload.get("description") or ""
                jobs.append(
                    NormalizedJob(
                        source=self.source_type,
                        company_id=company.id,
                        external_job_id=str(job_id),
                        requisition_id=str(job_id),
                        title=payload["title"],
                        location=location,
                        description_html=html,
                        description_text=text_from_html(html),
                        apply_url=url,
                        canonical_url=url,
                        employment_type=payload.get("employmentType"),
                        source_posted_at=parse_date(payload.get("datePosted")),
                        raw=payload,
                    )
                )
        if not jobs:
            raise ValueError("matching first-party pages contained no parseable JobPosting data")
        return jobs
