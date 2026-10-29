import asyncio

from bs4 import BeautifulSoup

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector


class WorkdayCollector(Collector):
    """Public CXS adapter; tenant-specific variants can subclass this collector."""

    source_type = "workday"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        host = f"{source.tenant}.{source.workday_cluster}.myworkdayjobs.com"
        base = f"https://{host}/wday/cxs/{source.tenant}/{source.site_id}"
        offset = 0
        limit = 20
        summaries: list[dict] = []
        while True:
            payload = await self.http.post_json(
                f"{base}/jobs",
                json={
                    "appliedFacets": {},
                    "limit": limit,
                    "offset": offset,
                    "searchText": "",
                },
            )
            page = payload.get("jobPostings", [])
            summaries.extend(page)
            offset += len(page)
            if not page or offset >= payload.get("total", 0):
                break

        async def fetch_detail(summary):
            path = summary.get("externalPath")
            if not path:
                return None
            detail = await self.http.get_json(f"{base}{path}")
            return summary, path, detail

        details = await asyncio.gather(*(fetch_detail(summary) for summary in summaries))
        jobs = []
        for result in details:
            if result is None:
                continue
            summary, path, detail = result
            info = detail.get("jobPostingInfo", detail)
            html = info.get("jobDescription") or ""
            apply_url = info.get("externalUrl") or f"https://{host}{path}"
            jobs.append(
                NormalizedJob(
                    source=self.source_type,
                    company_id=company.id,
                    external_job_id=str(
                        info.get("jobReqId") or summary.get("bulletFields", [path])[0]
                    ),
                    requisition_id=info.get("jobReqId"),
                    title=info.get("title") or summary["title"],
                    location=info.get("location") or summary.get("locationsText"),
                    description_html=html,
                    description_text=BeautifulSoup(html, "html.parser").get_text(" ", strip=True),
                    apply_url=apply_url,
                    canonical_url=apply_url,
                    raw=info,
                )
            )
        return jobs
