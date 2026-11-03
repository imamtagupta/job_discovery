import asyncio
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector
from .helpers import job_posting_jsonld, jsonld_location, parse_date, text_from_html


class IcimsCollector(Collector):
    source_type = "icims"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        links: set[str] = set()
        base = source.career_url.rstrip("/")
        for page in range(1, source.max_pages + 1):
            response = await self.http.request(
                "GET", f"{base}/jobs/search", params={"ss": 1, "pr": page}
            )
            found = {
                urljoin(str(response.url), anchor["href"])
                for anchor in BeautifulSoup(response.text, "html.parser").select(
                    "a[href*='/jobs/']"
                )
                if re.search(r"/jobs/\d+", anchor["href"])
            }
            before = len(links)
            links.update(found)
            if not found or len(links) == before:
                break
        if not links:
            raise ValueError("iCIMS search returned no public job links")

        async def fetch(url: str):
            return url, await self.http.request("GET", url)

        results = await asyncio.gather(*(fetch(url) for url in sorted(links)))
        jobs = []
        for url, response in results:
            payload = job_posting_jsonld(response.text)
            if not payload:
                continue
            match = re.search(r"/jobs/(\d+)", url)
            identifier = payload.get("identifier", {})
            job_id = identifier.get("value") if isinstance(identifier, dict) else identifier
            job_id = str(job_id or (match.group(1) if match else url))
            html = payload.get("description") or ""
            jobs.append(
                NormalizedJob(
                    source=self.source_type,
                    company_id=company.id,
                    external_job_id=job_id,
                    requisition_id=job_id,
                    title=payload["title"],
                    location=jsonld_location(payload),
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
            raise ValueError("iCIMS job pages contained no JobPosting data")
        return jobs
