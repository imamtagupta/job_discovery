import csv
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import (
    Application,
    AtsSource,
    CollectionError,
    CollectionRun,
    Company,
    Job,
    JobMatchScore,
)


def _csv_safe(value: object) -> object:
    """Prevent spreadsheet programs from interpreting imported text as formulas."""
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return f"'{value}"
    return value


def export_jobs_csv(session: Session, output: Path, since_hours: int | None = None) -> int:
    """Export active jobs with their latest score and application state."""
    scores = {item.job_id: item for item in session.scalars(select(JobMatchScore))}
    applications = {item.job_id: item for item in session.scalars(select(Application))}
    companies = {item.id: item.name for item in session.scalars(select(Company))}
    query = select(Job).where(Job.availability == "active")
    if since_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
        query = query.where(Job.first_seen_at >= cutoff)
    jobs = list(session.scalars(query.order_by(Job.first_seen_at.desc(), Job.id.desc())))
    fields = [
        "stable_job_id",
        "company",
        "title",
        "role_family",
        "primary_language",
        "location",
        "employment_type",
        "salary_min",
        "salary_max",
        "salary_currency",
        "work_authorization",
        "match_score",
        "application_priority",
        "eligibility_status",
        "matched_skills",
        "missing_skills",
        "posting_date",
        "first_discovered",
        "last_seen",
        "application_status",
        "source",
        "direct_apply_url",
    ]
    output.parent.mkdir(parents=True, exist_ok=True)
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for job in jobs:
            score = scores.get(job.id)
            application = applications.get(job.id)
            row = {
                "stable_job_id": job.stable_id,
                "company": companies.get(job.company_id, job.company_id),
                "title": job.title,
                "role_family": score.role_family if score else "Other",
                "primary_language": score.primary_language if score else "unknown",
                "location": job.location or "unknown",
                "employment_type": job.employment_type or "unknown",
                "salary_min": job.salary_min or "",
                "salary_max": job.salary_max or "",
                "salary_currency": job.salary_currency or "",
                "work_authorization": job.work_authorization,
                "match_score": score.score if score else "",
                "application_priority": score.application_priority if score else "",
                "eligibility_status": score.eligibility_status if score else "unknown",
                "matched_skills": ", ".join(score.matched_skills) if score else "",
                "missing_skills": ", ".join(score.missing_skills) if score else "",
                "posting_date": job.source_posted_at.isoformat() if job.source_posted_at else "",
                "first_discovered": job.first_seen_at.isoformat(),
                "last_seen": job.last_seen_at.isoformat(),
                "application_status": application.status if application else "not_applied",
                "source": job.source,
                "direct_apply_url": job.apply_url,
            }
            writer.writerow({key: _csv_safe(value) for key, value in row.items()})
    return len(jobs)


def build_discovery_report(
    session: Session,
    output: Path,
    companies_researched: int | None = None,
    since_hours: int | None = None,
) -> dict:
    latest = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()).limit(1))
    jobs_query = select(Job).where(Job.availability == "active")
    if since_hours is not None:
        cutoff = datetime.now(UTC) - timedelta(hours=since_hours)
        jobs_query = jobs_query.where(Job.first_seen_at >= cutoff)
    jobs = list(session.scalars(jobs_query))
    scores = {item.job_id: item for item in session.scalars(select(JobMatchScore))}
    companies = {item.id: item.name for item in session.scalars(select(Company))}
    sources = list(session.scalars(select(AtsSource).where(AtsSource.enabled.is_(True))))
    failures = 0
    if latest:
        failures = (
            session.scalar(
                select(func.count())
                .select_from(CollectionError)
                .where(CollectionError.run_id == latest.id)
            )
            or 0
        )
    active_ids = {job.id for job in jobs}
    family_counts = Counter(
        score.role_family
        for job_id, score in scores.items()
        if job_id in active_ids and not score.rejected
    )
    excluded = [
        score for job_id, score in scores.items() if job_id in active_ids and score.rejected
    ]
    exclusion_reasons = Counter(
        reason
        for score in excluded
        for reason in score.reasons
        if reason.startswith("Hard eligibility:")
    )
    ranked = sorted(
        ((job, scores[job.id]) for job in jobs if job.id in scores and not scores[job.id].rejected),
        key=lambda pair: (pair[1].application_priority, pair[1].score),
        reverse=True,
    )
    summary = {
        "total_companies_researched": companies_researched or len(companies),
        "verified_enabled_sources": len(sources),
        "successful_ats_collections": max(len(sources) - failures, 0),
        "failed_ats_collections": failures,
        "total_jobs_collected": len(jobs)
        if since_hours is not None
        else (latest.discovered_count if latest else 0),
        "unique_jobs": sum(job.duplicate_of_id is None for job in jobs),
        "python_backend_jobs": family_counts["Python Backend"],
        "platform_engineering_jobs": family_counts["Platform Engineering"]
        + family_counts["Cloud Infrastructure Software Engineering"],
        "ai_application_engineering_jobs": family_counts["AI Application Engineering"],
        "quant_fintech_engineering_jobs": family_counts["Quant / Financial Technology"],
        "jobs_scoring_80_plus": sum(
            job_id in active_ids and score.score >= 80 and not score.rejected
            for job_id, score in scores.items()
        ),
        "jobs_scoring_65_79": sum(
            job_id in active_ids and 65 <= score.score < 80 and not score.rejected
            for job_id, score in scores.items()
        ),
        "jobs_excluded": len(excluded),
    }
    lines = ["# Resume-Driven Job Discovery Report", "", "## Summary", ""]
    if since_hours is not None:
        lines.extend(
            [
                f"This report includes jobs first discovered in the last {since_hours} hours.",
                "A first-discovered timestamp is not necessarily the employer's posting date.",
                "",
            ]
        )
    lines.extend(f"- {key.replace('_', ' ').title()}: {value}" for key, value in summary.items())
    lines.extend(["", "## Exclusions", ""])
    lines.extend(f"- {reason}: {count}" for reason, count in exclusion_reasons.most_common())
    lines.extend(["", "## Top 25 Recommended Opportunities", ""])
    for index, (job, score) in enumerate(ranked[:25], 1):
        evidence = "; ".join(score.reasons[:4])
        lines.extend(
            [
                f"### {index}. {job.title.strip()} - {companies[job.company_id]}",
                "",
                f"- Score: {score.score}/100; priority: {score.application_priority}/100",
                f"- Role family: {score.role_family}; eligibility: {score.eligibility_status}",
                f"- Evidence: {evidence}",
                f"- Apply: {job.apply_url}",
                "",
            ]
        )
    output.write_text("\n".join(lines), encoding="utf-8")
    return summary
