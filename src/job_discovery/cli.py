import asyncio
import json
import logging
from pathlib import Path

import typer

from discovery.ats_detector import discover_registry, registry_summary, validate_registry

from .config import load_companies, load_yaml
from .database import (
    DatabaseConfigurationError,
    initialize_database,
    make_engine,
    session_factory,
)
from .pipeline import rescore_jobs, run_discovery, sync_source_definitions
from .services.applications import prepare_application_queue
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


if __name__ == "__main__":
    app()
