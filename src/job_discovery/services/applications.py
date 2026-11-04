import csv
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, Company, Job, JobMatchScore


def _csv_safe(value: object) -> object:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def prepare_application_queue(
    session: Session,
    output: Path,
    *,
    since_hours: int = 24,
    minimum_score: float = 80,
) -> int:
    """Queue strong new matches for review without submitting an application."""
    cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
    query = (
        select(Job, JobMatchScore, Company, Application)
        .join(JobMatchScore, JobMatchScore.job_id == Job.id)
        .join(Company, Company.id == Job.company_id)
        .outerjoin(Application, Application.job_id == Job.id)
        .where(
            Job.availability == "active",
            Job.first_seen_at >= cutoff,
            JobMatchScore.rejected.is_(False),
            JobMatchScore.eligibility_status == "eligible",
            JobMatchScore.score >= minimum_score,
        )
        .order_by(JobMatchScore.application_priority.desc(), JobMatchScore.score.desc())
    )
    rows = []
    for job, score, company, application in session.execute(query):
        if application is None:
            application = Application(job_id=job.id, status="ready_for_review")
            session.add(application)
        elif application.status not in {"not_applied", "ready_for_review"}:
            continue
        elif application.status == "not_applied":
            application.status = "ready_for_review"
        rows.append(
            {
                "stable_job_id": job.stable_id,
                "company": company.name,
                "title": job.title,
                "location": job.location or "unknown",
                "resume_match_score": score.score,
                "application_priority": score.application_priority,
                "status": application.status,
                "matched_skills": ", ".join(score.matched_skills),
                "review_reason": "; ".join(score.reasons[:4]),
                "direct_apply_url": job.apply_url,
            }
        )
    session.commit()
    fields = [
        "stable_job_id",
        "company",
        "title",
        "location",
        "resume_match_score",
        "application_priority",
        "status",
        "matched_skills",
        "review_reason",
        "direct_apply_url",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        writer.writerows({key: _csv_safe(value) for key, value in row.items()} for row in rows)
    return len(rows)
