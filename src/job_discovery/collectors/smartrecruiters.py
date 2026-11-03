from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector
from .helpers import parse_date, text_from_html


class SmartRecruitersCollector(Collector):
    source_type = "smartrecruiters"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        base = f"https://api.smartrecruiters.com/v1/companies/{source.company_identifier}/postings"
        offset = 0
        jobs = []
        while True:
            payload = await self.http.get_json(base, params={"limit": 100, "offset": offset})
            page = payload.get("content", [])
            for summary in page:
                item = await self.http.get_json(f"{base}/{summary['id']}")
                sections = item.get("jobAd", {}).get("sections", {})
                html = " ".join(
                    section.get("text", "")
                    for section in sections.values()
                    if isinstance(section, dict)
                )
                location = item.get("location", {})
                url = (
                    item.get("applyUrl")
                    or f"https://jobs.smartrecruiters.com/{source.company_identifier}/{item['id']}"
                )
                jobs.append(
                    NormalizedJob(
                        source=self.source_type,
                        company_id=company.id,
                        external_job_id=str(item["id"]),
                        requisition_id=item.get("refNumber"),
                        title=item["name"],
                        location=location.get("fullLocation") or location.get("city"),
                        description_html=html,
                        description_text=text_from_html(html),
                        apply_url=url,
                        canonical_url=url,
                        employment_type=item.get("typeOfEmployment", {}).get("label"),
                        source_posted_at=parse_date(item.get("releasedDate")),
                        raw=item,
                    )
                )
            offset += len(page)
            if not page or offset >= int(payload.get("totalFound", 0)):
                break
        return jobs
