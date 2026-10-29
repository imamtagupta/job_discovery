from datetime import datetime

from bs4 import BeautifulSoup

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector

REGIONAL_BASES = {
    "global": "https://api.lever.co/v0/postings",
    "eu": "https://api.eu.lever.co/v0/postings",
}


class LeverCollector(Collector):
    source_type = "lever"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        base = REGIONAL_BASES.get(source.region)
        if not base:
            raise ValueError(f"unsupported Lever region: {source.region}")
        payload = await self.http.get_json(f"{base}/{source.site}", params={"mode": "json"})
        jobs = []
        for item in payload:
            html = item.get("description") or item.get("descriptionPlain") or ""
            categories = item.get("categories") or {}
            created = item.get("createdAt")
            jobs.append(
                NormalizedJob(
                    source=self.source_type,
                    company_id=company.id,
                    external_job_id=str(item["id"]),
                    title=item["text"],
                    location=categories.get("location"),
                    description_html=html,
                    description_text=BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
                    apply_url=item.get("applyUrl") or item["hostedUrl"],
                    canonical_url=item.get("hostedUrl"),
                    employment_type=categories.get("commitment"),
                    source_posted_at=(
                        datetime.fromtimestamp(created / 1000).astimezone() if created else None
                    ),
                    raw=item,
                )
            )
        return jobs
