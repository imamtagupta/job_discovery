from datetime import datetime

from bs4 import BeautifulSoup

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector


def _timestamp(value: str | None) -> datetime | None:
    return datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


class GreenhouseCollector(Collector):
    source_type = "greenhouse"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        url = f"https://boards-api.greenhouse.io/v1/boards/{source.board_token}/jobs"
        payload = await self.http.get_json(url, params={"content": "true"})
        jobs = []
        for item in payload.get("jobs", []):
            html = item.get("content") or ""
            jobs.append(
                NormalizedJob(
                    source=self.source_type,
                    company_id=company.id,
                    external_job_id=str(item["id"]),
                    title=item["title"],
                    location=(item.get("location") or {}).get("name"),
                    description_html=html,
                    description_text=BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
                    apply_url=item["absolute_url"],
                    canonical_url=item["absolute_url"],
                    source_updated_at=_timestamp(item.get("updated_at")),
                    raw=item,
                )
            )
        return jobs
