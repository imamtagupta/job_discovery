from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector
from .helpers import parse_date, text_from_html


class OracleRecruitingCollector(Collector):
    source_type = "oracle-recruiting"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        host = source.oracle_host.removesuffix("/")
        endpoint = f"{host}/hcmRestApi/resources/latest/recruitingCEJobRequisitions"
        offset = 0
        jobs = []
        while True:
            payload = await self.http.get_json(
                endpoint,
                params={
                    "onlyData": "true",
                    "limit": 100,
                    "offset": offset,
                    "finder": (
                        f"findReqs;siteNumber={source.site_number},"
                        "facetsList=LOCATIONS;WORK_LOCATIONS"
                    ),
                },
            )
            page = payload.get("items", [])
            for item in page:
                job_id = str(item.get("Id") or item.get("RequisitionId"))
                html = " ".join(
                    str(item.get(key) or "")
                    for key in (
                        "ExternalDescriptionStr",
                        "ExternalResponsibilitiesStr",
                        "ExternalQualificationsStr",
                    )
                )
                url = f"{host}/hcmUI/CandidateExperience/en/sites/{source.site_number}/job/{job_id}"
                jobs.append(
                    NormalizedJob(
                        source=self.source_type,
                        company_id=company.id,
                        external_job_id=job_id,
                        requisition_id=item.get("RequisitionNumber"),
                        title=item["Title"],
                        location=item.get("PrimaryLocation"),
                        description_html=html,
                        description_text=text_from_html(html),
                        apply_url=url,
                        canonical_url=url,
                        source_posted_at=parse_date(item.get("ExternalPostedStartDate")),
                        raw=item,
                    )
                )
            offset += len(page)
            if not page or not payload.get("hasMore"):
                break
        return jobs
