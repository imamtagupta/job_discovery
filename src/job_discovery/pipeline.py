import logging
from datetime import UTC, datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from .collectors.base import AsyncHttpClient
from .collectors.registry import get_collector
from .config import CompaniesConfig
from .models import AtsSource, CollectionError, CollectionRun, Company, Job, JobMatchScore
from .notifications import ConsoleNotifier, Notifier
from .services.ingestion import (
    detect_duplicate,
    mark_missing_after_success,
    normalize_text,
    upsert_job,
)
from .services.scoring import score_job

logger = logging.getLogger(__name__)


def _apply_score(model: JobMatchScore, result) -> None:
    payload = result.model_dump()
    persistent = {
        "score",
        "role_family",
        "primary_language",
        "eligibility_status",
        "resume_relevance",
        "application_eligibility",
        "posting_freshness",
        "application_priority",
        "rejected",
        "matched_skills",
        "missing_skills",
        "breakdown",
        "reasons",
    }
    for key in persistent:
        setattr(model, key, payload[key])
    model.details = {key: value for key, value in payload.items() if key not in persistent}


def sync_source_definitions(session: Session, config: CompaniesConfig) -> None:
    for company in config.companies:
        model = session.get(Company, company.id)
        if model is None:
            model = Company(
                id=company.id, name=company.name, normalized_name=normalize_text(company.name)
            )
            session.add(model)
        model.name = company.name
        model.normalized_name = normalize_text(company.name)
        model.product_company = company.product_company
        model.enabled = company.enabled
        for source in company.sources:
            payload = source.model_dump(mode="json", exclude={"type", "enabled"}, exclude_none=True)
            current = session.scalar(
                select(AtsSource).where(
                    AtsSource.company_id == company.id,
                    AtsSource.source_type == source.type,
                )
            )
            if current is None:
                current = AtsSource(company_id=company.id, source_type=source.type)
                session.add(current)
            current.config = payload
            current.enabled = source.enabled and company.enabled
    session.commit()


async def run_discovery(
    session: Session,
    config: CompaniesConfig,
    profile: dict,
    scoring_config: dict,
    *,
    timeout: float = 20,
    per_host: int = 3,
    notifier: Notifier | None = None,
) -> CollectionRun:
    sync_source_definitions(session, config)
    run = CollectionRun()
    session.add(run)
    session.commit()
    http = AsyncHttpClient(timeout=timeout, per_host=per_host)
    new_high_quality: list[Job] = []
    try:
        for company in config.companies:
            if not company.enabled:
                continue
            for source_config in company.sources:
                if not source_config.enabled:
                    continue
                source = session.scalar(
                    select(AtsSource).where(
                        AtsSource.company_id == company.id,
                        AtsSource.source_type == source_config.type,
                    )
                )
                try:
                    collector = get_collector(source_config.type, http)
                    items = await collector.collect(company, source_config)
                    seen: set[str] = set()
                    for item in items:
                        job, change = upsert_job(session, run.id, item)
                        seen.add(item.external_job_id)
                        duplicate = detect_duplicate(session, job)
                        if duplicate and not job.duplicate_of_id:
                            job.duplicate_of_id = duplicate.id
                        result = score_job(job, profile, scoring_config)
                        score = session.scalar(
                            select(JobMatchScore).where(JobMatchScore.job_id == job.id)
                        )
                        if score is None:
                            score = JobMatchScore(job_id=job.id, score=0)
                            session.add(score)
                        _apply_score(score, result)
                        score.scored_at = datetime.now(UTC)
                        run.discovered_count += 1
                        run.new_count += change == "new"
                        run.updated_count += change == "updated"
                        if (
                            change == "new"
                            and result.score >= scoring_config.get("high_score_threshold", 70)
                            and not result.rejected
                        ):
                            new_high_quality.append(job)
                    mark_missing_after_success(session, company.id, source_config.type, seen)
                    source.last_success_at = datetime.now(UTC)
                    source.last_error = None
                    session.commit()
                except Exception as exc:
                    session.rollback()
                    logger.exception(
                        "collector_failed",
                        extra={"company": company.id, "source": source_config.type},
                    )
                    source = session.scalar(
                        select(AtsSource).where(
                            AtsSource.company_id == company.id,
                            AtsSource.source_type == source_config.type,
                        )
                    )
                    source.last_error = str(exc)
                    session.add(
                        CollectionError(
                            run_id=run.id,
                            source_id=source.id,
                            error_type=type(exc).__name__,
                            message=str(exc),
                        )
                    )
                    session.commit()
        run.status = (
            "completed_with_errors"
            if session.scalar(select(CollectionError).where(CollectionError.run_id == run.id))
            else "completed"
        )
    finally:
        await http.close()
        run.completed_at = datetime.now(UTC)
        session.commit()
    (notifier or ConsoleNotifier()).notify(new_high_quality)
    return run


def rescore_jobs(session: Session, profile: dict, scoring_config: dict) -> int:
    count = 0
    for job in session.scalars(select(Job)):
        result = score_job(job, profile, scoring_config)
        score = session.scalar(select(JobMatchScore).where(JobMatchScore.job_id == job.id))
        if score is None:
            score = JobMatchScore(job_id=job.id, score=0)
            session.add(score)
        _apply_score(score, result)
        score.scored_at = datetime.now(UTC)
        count += 1
    session.commit()
    return count
