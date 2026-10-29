import hashlib
import re
from datetime import UTC, datetime
from urllib.parse import urlsplit, urlunsplit

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Job, JobObservation
from ..schemas import NormalizedJob


def normalize_text(value: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def canonicalize_url(value: str | None) -> str | None:
    if not value:
        return None
    parts = urlsplit(value)
    return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path.rstrip("/"), "", ""))


def stable_job_id(job: NormalizedJob) -> str:
    identity = f"{job.source}\0{job.company_id}\0{job.external_job_id}"
    return hashlib.sha256(identity.encode()).hexdigest()


def content_hash(job: NormalizedJob) -> str:
    material = "\0".join([job.title, job.location or "", job.description_text, str(job.apply_url)])
    return hashlib.sha256(material.encode()).hexdigest()


def upsert_job(session: Session, run_id: int, item: NormalizedJob) -> tuple[Job, str]:
    now = datetime.now(UTC)
    existing = session.scalar(
        select(Job).where(
            Job.source == item.source,
            Job.company_id == item.company_id,
            Job.external_job_id == item.external_job_id,
        )
    )
    digest = content_hash(item)
    change = (
        "new"
        if existing is None
        else ("updated" if existing.description_hash != digest else "seen")
    )
    values = {
        "title": item.title,
        "normalized_title": normalize_text(item.title),
        "location": item.location,
        "normalized_location": normalize_text(item.location),
        "description_html": item.description_html,
        "description_text": item.description_text,
        "description_hash": digest,
        "apply_url": str(item.apply_url),
        "canonical_url": canonicalize_url(str(item.canonical_url or item.apply_url)),
        "requisition_id": item.requisition_id,
        "employment_type": item.employment_type,
        "salary_min": item.salary_min,
        "salary_max": item.salary_max,
        "salary_currency": item.salary_currency,
        "source_posted_at": item.source_posted_at,
        "source_updated_at": item.source_updated_at,
        "last_seen_at": now,
        "availability": "active",
        "work_authorization": item.work_authorization.value,
        "raw": item.raw,
    }
    if existing is None:
        existing = Job(
            stable_id=stable_job_id(item),
            source=item.source,
            company_id=item.company_id,
            external_job_id=item.external_job_id,
            first_seen_at=now,
            **values,
        )
        session.add(existing)
        session.flush()
    else:
        for key, value in values.items():
            setattr(existing, key, value)
    session.add(
        JobObservation(job_id=existing.id, run_id=run_id, change_type=change, content_hash=digest)
    )
    return existing, change


def mark_missing_after_success(
    session: Session, company_id: str, source: str, observed_ids: set[str]
) -> int:
    jobs = session.scalars(
        select(Job).where(
            Job.company_id == company_id, Job.source == source, Job.availability == "active"
        )
    )
    changed = 0
    for job in jobs:
        if job.external_job_id not in observed_ids:
            job.availability = "missing"
            changed += 1
    return changed


def detect_duplicate(session: Session, job: Job) -> Job | None:
    candidates = session.scalars(
        select(Job).where(Job.company_id == job.company_id, Job.id != job.id)
    )
    for candidate in candidates:
        if job.requisition_id and candidate.requisition_id == job.requisition_id:
            return candidate
        if job.canonical_url and candidate.canonical_url == job.canonical_url:
            return candidate
        # Title/location similarity is useful for review, but is not strong enough to link
        # automatically: one company can legitimately have parallel openings.
    return None
