import asyncio
import email.utils
import time
from abc import ABC, abstractmethod
from collections import defaultdict
from datetime import UTC, datetime
from urllib.parse import urlparse

import httpx
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential

from ..config import CompanyConfig, SourceConfig
from ..schemas import NormalizedJob


class RetryableHttpError(Exception):
    pass


class AsyncHttpClient:
    def __init__(
        self, timeout: float = 20, per_host: int = 3, min_interval: float = 1.0, transport=None
    ):
        self.client = httpx.AsyncClient(timeout=timeout, follow_redirects=True, transport=transport)
        self.per_host = per_host
        self.min_interval = max(min_interval, 0.0)
        self._limits: dict[str, asyncio.Semaphore] = defaultdict(
            lambda: asyncio.Semaphore(self.per_host)
        )
        self._last_request_at: dict[str, float] = {}

    async def close(self) -> None:
        await self.client.aclose()

    async def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        """Make a rate-limited request with bounded retry semantics."""
        return await self._request(method, url, **kwargs)

    async def _request(self, method: str, url: str, **kwargs) -> httpx.Response:
        # GETs are idempotent and may be retried after transient throttling.
        # POSTs are intentionally single-attempt to avoid duplicate writes.
        if method.upper() == "GET":
            return await self._request_get_with_retry(method, url, **kwargs)
        return await self._request_once(method, url, **kwargs)

    @retry(
        retry=retry_if_exception_type((httpx.TransportError, RetryableHttpError)),
        stop=stop_after_attempt(5),
        wait=wait_exponential(multiplier=2, min=2, max=60),
        reraise=True,
    )
    async def _request_get_with_retry(
        self, method: str, url: str, **kwargs
    ) -> httpx.Response:
        return await self._request_once(method, url, **kwargs)

    async def _request_once(self, method: str, url: str, **kwargs) -> httpx.Response:
        host = urlparse(url).netloc
        async with self._limits[host]:
            interval = self.min_interval if method.upper() == "POST" else 0.0
            elapsed = time.monotonic() - self._last_request_at.get(host, 0.0)
            if elapsed < interval:
                await asyncio.sleep(interval - elapsed)
            response = await self.client.request(method, url, **kwargs)
            self._last_request_at[host] = time.monotonic()
        if response.status_code == 429:
            retry_after = response.headers.get("Retry-After")
            if retry_after:
                try:
                    delay = min(float(retry_after), 30)
                except ValueError:
                    parsed = email.utils.parsedate_to_datetime(retry_after)
                    delay = min(max((parsed - datetime.now(UTC)).total_seconds(), 0), 30)
                await asyncio.sleep(delay)
            raise RetryableHttpError("HTTP 429 rate limited")
        if response.status_code >= 500:
            raise RetryableHttpError(f"HTTP {response.status_code}")
        response.raise_for_status()
        return response

    async def get_json(self, url: str, **kwargs) -> dict | list:
        response = await self._request("GET", url, **kwargs)
        return response.json()

    async def post_json(self, url: str, **kwargs) -> dict | list:
        response = await self._request("POST", url, **kwargs)
        return response.json()


class Collector(ABC):
    source_type: str

    def __init__(self, http: AsyncHttpClient):
        self.http = http

    @abstractmethod
    async def collect(
        self, company: CompanyConfig, source: SourceConfig
    ) -> list[NormalizedJob]: ...
