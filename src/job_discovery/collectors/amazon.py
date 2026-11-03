from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector
from .helpers import parse_date, text_from_html


class AmazonCollector(Collector):
    source_type = "amazon"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        offset = 0
        limit = 100
        jobs = []
        while True:
            payload = await self.http.get_json(
                "https://www.amazon.jobs/en/search.json",
                params={
                    "base_query": "",
                    "loc_query": source.location_filter or "India",
                    "offset": offset,
                    "result_limit": limit,
                    "sort": "recent",
                },
            )
            page = payload.get("jobs", [])
            for item in page:
                if source.location_filter == "India" and item.get("country_code") != "IND":
                    continue
                html = item.get("description") or item.get("description_short") or ""
                url = f"https://www.amazon.jobs{item['job_path']}"
                job_id = str(item.get("id_icims") or item["id"])
                jobs.append(
                    NormalizedJob(
                        source=self.source_type,
                        company_id=company.id,
                        external_job_id=job_id,
                        requisition_id=job_id,
                        title=item["title"],
                        location=item.get("normalized_location") or item.get("location"),
                        description_html=html,
                        description_text=text_from_html(html),
                        apply_url=url,
                        canonical_url=url,
                        employment_type=item.get("job_schedule_type"),
                        source_posted_at=parse_date(item.get("posted_date")),
                        raw=item,
                    )
                )
            offset += len(page)
            if not page or offset >= int(payload.get("hits", 0)):
                break
        return jobs
