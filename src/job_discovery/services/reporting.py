from collections import Counter
from pathlib import Path

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from ..models import AtsSource, CollectionError, CollectionRun, Company, Job, JobMatchScore


def build_discovery_report(
    session: Session, output: Path, companies_researched: int | None = None
) -> dict:
    latest = session.scalar(select(CollectionRun).order_by(CollectionRun.id.desc()).limit(1))
    jobs = list(session.scalars(select(Job).where(Job.availability == "active")))
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
    excluded = [score for score in scores.values() if score.rejected]
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
        "total_jobs_collected": latest.discovered_count if latest else 0,
        "unique_jobs": sum(job.duplicate_of_id is None for job in jobs),
        "python_backend_jobs": family_counts["Python Backend"],
        "platform_engineering_jobs": family_counts["Platform Engineering"]
        + family_counts["Cloud Infrastructure Software Engineering"],
        "ai_application_engineering_jobs": family_counts["AI Application Engineering"],
        "quant_fintech_engineering_jobs": family_counts["Quant / Financial Technology"],
        "jobs_scoring_80_plus": sum(
            score.score >= 80 and not score.rejected for score in scores.values()
        ),
        "jobs_scoring_65_79": sum(
            65 <= score.score < 80 and not score.rejected for score in scores.values()
        ),
        "jobs_excluded": len(excluded),
    }
    lines = ["# Resume-Driven Job Discovery Report", "", "## Summary", ""]
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
