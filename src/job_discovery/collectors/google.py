import asyncio
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector


class GoogleCollector(Collector):
    source_type = "google"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        links: set[str] = set()
        for page in range(1, source.max_pages + 1):
            response = await self.http.request(
                "GET",
                source.career_url,
                params={"location": source.location_filter or "India", "page": page},
            )
            found = {
                urljoin(str(response.url), anchor["href"].split("?", 1)[0])
                for anchor in BeautifulSoup(response.text, "html.parser").select(
                    "a[href*='jobs/results/']"
                )
            }
            before = len(links)
            links.update(found)
            if not found or len(links) == before:
                break
        if not links:
            raise ValueError("Google Careers search returned no public job links")

        async def fetch(url: str):
            return url, await self.http.request("GET", url)

        jobs = []
        for start in range(0, len(links), 50):
            results = await asyncio.gather(
                *(fetch(url) for url in sorted(links)[start : start + 50])
            )
            for url, response in results:
                soup = BeautifulSoup(response.text, "html.parser")
                heading = soup.select_one("h2.p1N2lc") or soup.select_one(
                    "meta[property='og:title']"
                )
                title = (
                    heading.get("content")
                    if heading and heading.name == "meta"
                    else (heading.get_text(" ", strip=True) if heading else None)
                )
                match = re.search(r"/results/(\d+)", url)
                if not title or not match:
                    continue
                location_node = soup.select_one(".pwO9Dc")
                body = soup.select_one(".DkhPwc") or soup.select_one("main")
                description = body.get_text(" ", strip=True) if body else ""
                jobs.append(
                    NormalizedJob(
                        source=self.source_type,
                        company_id=company.id,
                        external_job_id=match.group(1),
                        requisition_id=match.group(1),
                        title=title,
                        location=location_node.get_text(" ", strip=True) if location_node else None,
                        description_html=str(body) if body else None,
                        description_text=description,
                        apply_url=url,
                        canonical_url=url,
                        raw={"url": url},
                    )
                )
        if not jobs:
            raise ValueError("Google Careers pages contained no parseable jobs")
        return jobs
