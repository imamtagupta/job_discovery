from sqlalchemy import select
from sqlalchemy.orm import Session

from job_discovery.database import initialize_database, make_engine
from job_discovery.models import CollectionRun, Company, JobObservation
from job_discovery.schemas import NormalizedJob
from job_discovery.services.ingestion import mark_missing_after_success, upsert_job


def test_idempotent_upsert_and_history():
    engine = make_engine("sqlite:///:memory:")
    initialize_database(engine)
    with Session(engine) as session:
        session.add(Company(id="acme", name="Acme", normalized_name="acme"))
        run1 = CollectionRun()
        session.add(run1)
        session.flush()
        item = NormalizedJob(
            source="greenhouse",
            company_id="acme",
            external_job_id="1",
            title="Backend Engineer",
            apply_url="https://example.com/1",
            description_text="Python",
        )
        job, change = upsert_job(session, run1.id, item)
        assert change == "new"
        session.commit()
        first_seen = job.first_seen_at
        run2 = CollectionRun()
        session.add(run2)
        session.flush()
        same_job, change = upsert_job(session, run2.id, item)
        session.commit()
        assert same_job.id == job.id
        assert same_job.first_seen_at == first_seen
        assert change == "seen"
        assert len(list(session.scalars(select(JobObservation)))) == 2


def test_missing_only_after_successful_source_result():
    engine = make_engine("sqlite:///:memory:")
    initialize_database(engine)
    with Session(engine) as session:
        session.add(Company(id="acme", name="Acme", normalized_name="acme"))
        run = CollectionRun()
        session.add(run)
        session.flush()
        item = NormalizedJob(
            source="lever",
            company_id="acme",
            external_job_id="1",
            title="Engineer",
            apply_url="https://example.com/1",
        )
        job, _ = upsert_job(session, run.id, item)
        assert mark_missing_after_success(session, "acme", "lever", set()) == 1
        assert job.availability == "missing"
