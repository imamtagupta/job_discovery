from collections.abc import Callable

from .amazon import AmazonCollector
from .apple import AppleCollector
from .ashby import AshbyCollector
from .base import AsyncHttpClient, Collector
from .google import GoogleCollector
from .greenhouse import GreenhouseCollector
from .icims import IcimsCollector
from .lever import LeverCollector
from .oracle import OracleRecruitingCollector
from .sitemap import SitemapCollector
from .smartrecruiters import SmartRecruitersCollector
from .workday import WorkdayCollector

COLLECTORS: dict[str, Callable[[AsyncHttpClient], Collector]] = {
    "greenhouse": GreenhouseCollector,
    "lever": LeverCollector,
    "ashby": AshbyCollector,
    "workday": WorkdayCollector,
    "smartrecruiters": SmartRecruitersCollector,
    "oracle-recruiting": OracleRecruitingCollector,
    "icims": IcimsCollector,
    "amazon": AmazonCollector,
    "apple": AppleCollector,
    "google": GoogleCollector,
    "sitemap": SitemapCollector,
}


def get_collector(source_type: str, http: AsyncHttpClient) -> Collector:
    try:
        return COLLECTORS[source_type](http)
    except KeyError as exc:
        raise ValueError(f"collector not implemented: {source_type}") from exc
