import json
from datetime import datetime
from typing import Any

from bs4 import BeautifulSoup


def text_from_html(value: str | None) -> str:
    return BeautifulSoup(value or "", "html.parser").get_text(" ", strip=True)


def parse_date(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def job_posting_jsonld(html: str) -> dict[str, Any] | None:
    soup = BeautifulSoup(html, "html.parser")
    for script in soup.select('script[type="application/ld+json"]'):
        try:
            payload = json.loads(script.string or script.get_text())
        except (TypeError, json.JSONDecodeError):
            continue
        values = payload if isinstance(payload, list) else [payload]
        for value in values:
            if isinstance(value, dict) and value.get("@type") == "JobPosting":
                return value
            if isinstance(value, dict):
                for node in value.get("@graph", []):
                    if isinstance(node, dict) and node.get("@type") == "JobPosting":
                        return node
    return None


def jsonld_location(payload: dict[str, Any]) -> str | None:
    locations = payload.get("jobLocation") or []
    if isinstance(locations, dict):
        locations = [locations]
    rendered = []
    for location in locations:
        address = location.get("address", {}) if isinstance(location, dict) else {}
        parts = [address.get(key) for key in ("addressLocality", "addressRegion", "addressCountry")]
        value = ", ".join(str(part) for part in parts if part)
        if value:
            rendered.append(value)
    return "; ".join(rendered) or None
