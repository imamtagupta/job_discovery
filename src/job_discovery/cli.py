import asyncio
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import typer
from sqlalchemy import select

from discovery.ats_detector import discover_registry, registry_summary, validate_registry

from .config import load_companies, load_yaml
from .database import (
    DatabaseConfigurationError,
    initialize_database,
    make_engine,
    session_factory,
)
from .models import Application, Job
from .pipeline import rescore_jobs, run_discovery, sync_source_definitions
from .services.application_faq import add_faq_entry, record_unseen_question, resolve_question
from .services.applications import prepare_application_queue
from .services.browser_applications import (
    load_discovered_jobs,
    load_queue_job,
    stage_application,
    write_staging_result,
)
from .services.reporting import build_discovery_report, export_jobs_csv
from .services.sheets import SheetsSync
from .settings import Settings

app = typer.Typer(no_args_is_help=True)


def _print_source_summary(summary: dict[str, int]) -> None:
    typer.echo(json.dumps(summary, indent=2, sort_keys=True))


def context():
    settings = Settings()
    logging.basicConfig(
        level=settings.log_level,
        format="%(asctime)s level=%(levelname)s logger=%(name)s message=%(message)s",
    )
    try:
        engine = make_engine(settings.database_url)
    except DatabaseConfigurationError as exc:
        raise typer.BadParameter(str(exc), param_hint="DATABASE_URL") from None
    return settings, engine, session_factory(engine)


@app.command("init-db")
def init_db() -> None:
    """Create database tables and load configured companies/sources."""
    settings, engine, factory = context()
    initialize_database(engine)
    with factory() as session:
        sync_source_definitions(session, load_companies(settings.companies_config))
    typer.echo("Database initialized.")


@app.command("validate-config")
def validate_config() -> None:
    """Validate company, profile, and scoring configuration."""
    settings, _, _ = context()
    companies = load_companies(settings.companies_config)
    load_yaml(settings.profile_config)
    load_yaml(settings.scoring_config)
    load_yaml(settings.application_faq_config)
    enabled = sum(c.enabled for c in companies.companies)
    typer.echo(f"Configuration valid: {len(companies.companies)} companies ({enabled} enabled).")


@app.command("discover")
def discover() -> None:
    """Run one isolated, incremental collection cycle."""
    settings, engine, factory = context()
    initialize_database(engine)
    with factory() as session:
        run = asyncio.run(
            run_discovery(
                session,
                load_companies(settings.companies_config),
                load_yaml(settings.profile_config),
                load_yaml(settings.scoring_config),
                timeout=settings.http_timeout_seconds,
                per_host=settings.http_per_host_concurrency,
            )
        )
        typer.echo(
            json.dumps(
                {
                    "run_id": run.id,
                    "status": run.status,
                    "discovered": run.discovered_count,
                    "new": run.new_count,
                    "updated": run.updated_count,
                }
            )
        )


@app.command("rescore")
def rescore() -> None:
    """Recompute scores for stored jobs without collecting."""
    settings, engine, factory = context()
    initialize_database(engine)
    with factory() as session:
        count = rescore_jobs(
            session, load_yaml(settings.profile_config), load_yaml(settings.scoring_config)
        )
    typer.echo(f"Rescored {count} jobs.")


@app.command("sync-sheets")
def sync_sheets() -> None:
    """Incrementally synchronize Google Sheets."""
    settings, engine, factory = context()
    if not settings.google_sheets_spreadsheet_id or not settings.google_service_account_json:
        raise typer.BadParameter(
            "GOOGLE_SHEETS_SPREADSHEET_ID and GOOGLE_SERVICE_ACCOUNT_JSON are required"
        )
    with factory() as session:
        SheetsSync(
            settings.google_sheets_spreadsheet_id, settings.google_service_account_json
        ).sync(session, settings.new_match_threshold)
    typer.echo("Google Sheets synchronized.")


@app.command("discover-sources")
def discover_sources() -> None:
    """Discover ATS links from official career pages and validate public endpoints."""
    settings, _, _ = context()
    summary = asyncio.run(
        discover_registry(
            settings.target_companies_config,
            settings.companies_config,
            settings.pending_sources_config,
            settings.source_health_config,
            timeout=settings.http_timeout_seconds,
            per_host=settings.http_per_host_concurrency,
        )
    )
    _print_source_summary(summary)


@app.command("validate-sources")
def validate_sources() -> None:
    """Revalidate configured and pending ATS identifiers."""
    settings, _, _ = context()
    summary = asyncio.run(
        validate_registry(
            settings.companies_config,
            settings.pending_sources_config,
            timeout=settings.http_timeout_seconds,
            per_host=settings.http_per_host_concurrency,
        )
    )
    _print_source_summary(summary)


@app.command("list-sources")
def list_sources() -> None:
    """Show registry discovery and validation totals without network access."""
    settings, _, _ = context()
    _print_source_summary(
        registry_summary(
            settings.target_companies_config,
            settings.companies_config,
            settings.pending_sources_config,
            settings.source_health_config,
        )
    )


@app.command("report")
def report(
    output: str = "discovery_report.md",
    since_hours: int | None = typer.Option(None, min=1),
) -> None:
    """Generate an evidence-based report from the latest stored collection."""
    settings, engine, factory = context()
    initialize_database(engine)
    with factory() as session:
        researched = len(load_yaml(settings.target_companies_config).get("companies", []))
        summary = build_discovery_report(session, Path(output), researched, since_hours)
    _print_source_summary(summary)


@app.command("export-jobs")
def export_jobs(
    output: str = "jobs.csv",
    since_hours: int | None = typer.Option(None, min=1),
) -> None:
    """Export all active fetched jobs to a spreadsheet-safe CSV file."""
    _, engine, factory = context()
    initialize_database(engine)
    with factory() as session:
        count = export_jobs_csv(session, Path(output), since_hours)
    typer.echo(f"Exported {count} active jobs to {output}.")


@app.command("prepare-applications")
def prepare_applications(
    output: str = "application-queue.csv",
    since_hours: int = typer.Option(24, min=1),
    minimum_score: float = typer.Option(80, min=0, max=100),
) -> None:
    """Create an approval queue for strong new matches; never submits applications."""
    _, engine, factory = context()
    initialize_database(engine)
    with factory() as session:
        count = prepare_application_queue(
            session,
            Path(output),
            since_hours=since_hours,
            minimum_score=minimum_score,
        )
    typer.echo(f"Prepared {count} jobs for application review in {output}.")


@app.command("stage-application")
def stage_application_command(
    job_id: str | None = typer.Option(None, help="Stable job id; defaults to first ready job."),
    queue: str = typer.Option("application-queue.csv"),
    resume: str = typer.Option("MamtaGupta2026.1.pdf"),
    artifacts: str = typer.Option("application-artifacts"),
    headless: bool = typer.Option(False, help="Run without a visible Chromium window."),
) -> None:
    """Fill one reviewed application, capture questions, and stop before submission."""
    settings, _, _ = context()
    queue_path = Path(queue)
    resume_path = Path(resume)
    if not resume_path.is_file():
        raise typer.BadParameter(f"Resume not found: {resume_path}", param_hint="resume")
    try:
        row = load_queue_job(queue_path, job_id)
    except (OSError, ValueError) as exc:
        raise typer.BadParameter(str(exc), param_hint="queue") from None
    artifact_dir = Path(artifacts)
    result = stage_application(
        row,
        profile=load_yaml(settings.profile_config),
        faq_path=settings.application_faq_config,
        backlog_path=settings.application_question_backlog,
        resume_path=resume_path,
        artifact_dir=artifact_dir,
        headless=headless,
    )
    report_path = write_staging_result(result, artifact_dir)
    typer.echo(json.dumps({**result.__dict__, "report": str(report_path)}, indent=2))
    if result.status == "failed":
        raise typer.Exit(code=1)


@app.command("apply-from-files")
def apply_from_files(
    report: str = typer.Option("discovery-report.md"),
    jobs: str = typer.Option("fetched-jobs.csv"),
    resume: str = typer.Option("MamtaGupta2026.1.pdf"),
    artifacts: str = typer.Option("application-artifacts"),
    limit: int = typer.Option(25, min=1),
    include_needs_review: bool = typer.Option(
        False, help="Also process report jobs whose eligibility still needs review."
    ),
    job_id: Annotated[
        list[str] | None, typer.Option(help="Only process these stable job ids.")
    ] = None,
    submit: bool = typer.Option(False, help="Click Submit only when no answers are unresolved."),
) -> None:
    """Process report recommendations first, then remaining eligible fetched jobs."""
    settings, engine, factory = context()
    initialize_database(engine)
    resume_path = Path(resume)
    if not resume_path.is_file():
        raise typer.BadParameter(f"Resume not found: {resume_path}", param_hint="resume")
    try:
        rows = load_discovered_jobs(
            Path(report), Path(jobs), include_needs_review=include_needs_review
        )
        if job_id:
            requested = set(job_id)
            rows = [row for row in rows if row["stable_job_id"] in requested]
        rows = rows[:limit]
    except OSError as exc:
        raise typer.BadParameter(str(exc), param_hint="report/jobs") from None

    artifact_dir = Path(artifacts)
    totals: dict[str, int] = {}
    profile = load_yaml(settings.profile_config)
    with factory() as session:
        for row in rows:
            result = stage_application(
                row,
                profile=profile,
                faq_path=settings.application_faq_config,
                backlog_path=settings.application_question_backlog,
                resume_path=resume_path,
                artifact_dir=artifact_dir,
                headless=True,
                submit=submit,
            )
            write_staging_result(result, artifact_dir)
            totals[result.status] = totals.get(result.status, 0) + 1

            job = session.scalar(select(Job).where(Job.stable_id == row["stable_job_id"]))
            if job is None:
                continue
            application = session.scalar(select(Application).where(Application.job_id == job.id))
            if application is None:
                application = Application(job_id=job.id)
                session.add(application)
            application.status = result.status
            application.notes = result.error or "; ".join(result.unresolved_questions)
            if result.status == "submitted":
                application.applied_at = datetime.now(UTC)
            session.commit()
    typer.echo(json.dumps({"selected": len(rows), "results": totals}, sort_keys=True))


@app.command("faq-check")
def faq_check(
    question: str,
    source: str | None = typer.Option(None),
) -> None:
    """Resolve a form question from approved answers or record it for review."""
    settings, _, _ = context()
    result = resolve_question(settings.application_faq_config, question)
    if result["status"] == "unseen":
        record_unseen_question(settings.application_question_backlog, question, source)
    typer.echo(json.dumps(result, indent=2, sort_keys=True))


@app.command("faq-add")
def faq_add(
    key: str,
    question: str,
    answer: str,
    alias: Annotated[list[str] | None, typer.Option()] = None,
) -> None:
    """Add a user-approved answer for future application forms."""
    settings, _, _ = context()
    try:
        add_faq_entry(
            settings.application_faq_config,
            key=key,
            question=question,
            answer=answer,
            aliases=alias,
        )
    except ValueError as exc:
        raise typer.BadParameter(str(exc), param_hint="key") from None
    typer.echo(f"Added approved FAQ answer: {key}")


if __name__ == "__main__":
    app()
