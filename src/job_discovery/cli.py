import asyncio
import json
import logging
import shutil
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
from .models import ApplicationAttempt, ApplicationQuestionAudit, Job
from .pipeline import rescore_jobs, run_discovery, sync_source_definitions
from .services.applications import (
    load_application_form,
    load_knowledge_base,
    prepare_application,
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


@app.command("init-application-kb")
def init_application_kb() -> None:
    """Create a private application-answer knowledge base from the safe example."""
    settings = Settings()
    destination = settings.application_answers_config
    if destination.exists():
        raise typer.BadParameter(f"Refusing to overwrite existing {destination}")
    destination.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(settings.application_answers_example, destination)
    typer.echo(f"Created private knowledge base at {destination}.")


@app.command("validate-application-kb")
def validate_application_kb() -> None:
    """Validate answer policies, scopes, expiry dates, and entry uniqueness."""
    settings = Settings()
    knowledge_base = load_knowledge_base(settings.application_answers_config)
    enabled = sum(entry.enabled for entry in knowledge_base.entries)
    autonomous = sum(entry.enabled and entry.autonomous_use for entry in knowledge_base.entries)
    typer.echo(
        f"Application knowledge base valid: {enabled} enabled entries, "
        f"{autonomous} approved for autonomous fill."
    )


@app.command("prepare-application")
def prepare_application_command(
    questions: Annotated[Path, typer.Option(exists=True, readable=True)],
    output: Annotated[Path, typer.Option()] = Path("application_preview.json"),
) -> None:
    """Resolve extracted form questions and create an audited application preview."""
    settings, engine, factory = context()
    initialize_database(engine)
    form = load_application_form(questions)
    answers = prepare_application(form, load_knowledge_base(settings.application_answers_config))
    counts = {
        status: sum(answer.status == status for answer in answers)
        for status in ("auto_filled", "needs_review", "unanswered")
    }
    preview = {
        "job_stable_id": form.job_stable_id,
        "company": form.company,
        "country": form.country,
        "source_url": form.source_url,
        "status": (
            "ready"
            if not counts["needs_review"] and not counts["unanswered"]
            else "needs_review"
        ),
        "counts": counts,
        "answers": [answer.model_dump(mode="json") for answer in answers],
    }
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(preview, indent=2), encoding="utf-8")
    with factory() as session:
        job = None
        if form.job_stable_id:
            job = session.scalar(select(Job).where(Job.stable_id == form.job_stable_id))
            if job is None:
                raise typer.BadParameter(f"Unknown job stable ID: {form.job_stable_id}")
        attempt = ApplicationAttempt(
            job_id=job.id if job else None,
            source_url=form.source_url,
            status=preview["status"],
            auto_filled_count=counts["auto_filled"],
            review_count=counts["needs_review"],
            unanswered_count=counts["unanswered"],
            preview=preview,
        )
        session.add(attempt)
        session.flush()
        for answer in answers:
            session.add(
                ApplicationQuestionAudit(
                    attempt_id=attempt.id,
                    question_id=answer.question_id,
                    question=answer.question,
                    status=answer.status,
                    answer={"value": answer.answer} if answer.answer is not None else None,
                    knowledge_key=answer.knowledge_key,
                    confidence=answer.confidence,
                    reason=answer.reason,
                    evidence=answer.evidence,
                    answer_fingerprint=answer.answer_fingerprint,
                )
            )
        session.commit()
    typer.echo(json.dumps({"attempt_id": attempt.id, "output": str(output), **counts}))


if __name__ == "__main__":
    app()
