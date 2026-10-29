from datetime import datetime

from bs4 import BeautifulSoup

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector


class AshbyCollector(Collector):
    source_type = "ashby"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        url = f"https://api.ashbyhq.com/posting-api/job-board/{source.board_name}"
        payload = await self.http.get_json(url)
        jobs = []
        for item in payload.get("jobs", []):
            html = item.get("descriptionHtml") or item.get("descriptionPlain") or ""
            published = item.get("publishedAt")
            jobs.append(
                NormalizedJob(
                    source=self.source_type,
                    company_id=company.id,
                    external_job_id=str(item["id"]),
                    title=item["title"],
                    location=item.get("location"),
                    description_html=html,
                    description_text=BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
                    apply_url=item["jobUrl"],
                    canonical_url=item["jobUrl"],
                    employment_type=item.get("employmentType"),
                    source_posted_at=(
                        datetime.fromisoformat(published.replace("Z", "+00:00"))
                        if published
                        else None
                    ),
                    raw=item,
                )
            )
        return jobs
