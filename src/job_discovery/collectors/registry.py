from collections.abc import Callable

from .ashby import AshbyCollector
from .base import AsyncHttpClient, Collector
from .greenhouse import GreenhouseCollector
from .lever import LeverCollector
from .workday import WorkdayCollector

COLLECTORS: dict[str, Callable[[AsyncHttpClient], Collector]] = {
    "greenhouse": GreenhouseCollector,
    "lever": LeverCollector,
    "ashby": AshbyCollector,
    "workday": WorkdayCollector,
}


def get_collector(source_type: str, http: AsyncHttpClient) -> Collector:
    try:
        return COLLECTORS[source_type](http)
    except KeyError as exc:
        raise ValueError(f"collector not implemented: {source_type}") from exc
