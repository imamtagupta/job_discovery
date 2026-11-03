import asyncio
import re
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob
from .base import Collector
from .helpers import job_posting_jsonld, jsonld_location, parse_date, text_from_html


class AppleCollector(Collector):
    source_type = "apple"

    async def collect(self, company: CompanyConfig, source: SourceConfig) -> list[NormalizedJob]:
        base = source.career_url or "https://jobs.apple.com/en-in/search?location=india-INDC"
        links: set[str] = set()
        for page in range(1, source.max_pages + 1):
            response = await self.http.request("GET", base, params={"page": page})
            found = {
                urljoin(str(response.url), anchor["href"].split("?", 1)[0])
                for anchor in BeautifulSoup(response.text, "html.parser").select(
                    "a[href*='/details/']"
                )
                if not anchor["href"].endswith("/locationPicker")
            }
            before = len(links)
            links.update(found)
            if not found or len(links) == before:
                break

        async def fetch(url: str):
            return url, await self.http.request("GET", url)

        results = await asyncio.gather(*(fetch(url) for url in sorted(links)))
        jobs = []
        for url, response in results:
            payload = job_posting_jsonld(response.text)
            if payload:
                title = payload.get("title")
                html = payload.get("description") or ""
                location = jsonld_location(payload)
                posted = parse_date(payload.get("datePosted"))
                raw = payload
            else:
                soup = BeautifulSoup(response.text, "html.parser")
                title = (soup.select_one("h1") or soup.title).get_text(" ", strip=True)
                body = soup.select_one("main") or soup
                html = str(body)
                location = None
                posted = None
                raw = {}
            match = re.search(r"/details/(\d+)/", url)
            if not match or not title:
                continue
            jobs.append(
                NormalizedJob(
                    source=self.source_type,
                    company_id=company.id,
                    external_job_id=match.group(1),
                    requisition_id=match.group(1),
                    title=title,
                    location=location,
                    description_html=html,
                    description_text=text_from_html(html),
                    apply_url=url,
                    canonical_url=url,
                    source_posted_at=posted,
                    raw=raw,
                )
            )
        return jobs
