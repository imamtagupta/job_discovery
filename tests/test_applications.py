from sqlalchemy import select
from sqlalchemy.orm import Session

from job_discovery.database import initialize_database, make_engine
from job_discovery.models import Application, CollectionRun, Company, JobMatchScore
from job_discovery.schemas import NormalizedJob
from job_discovery.services.applications import prepare_application_queue
from job_discovery.services.ingestion import upsert_job


def test_prepare_application_queue_only_includes_approved_candidates(tmp_path):
    engine = make_engine("sqlite:///:memory:")
    initialize_database(engine)
    with Session(engine) as session:
        session.add(Company(id="acme", name="Acme", normalized_name="acme"))
        run = CollectionRun()
        session.add(run)
        session.flush()
        strong, _ = upsert_job(
            session,
            run.id,
            NormalizedJob(
                source="test",
                company_id="acme",
                external_job_id="1",
                title="Senior Python Backend Engineer",
                location="Mumbai, India",
                apply_url="https://example.com/1",
            ),
        )
        rejected, _ = upsert_job(
            session,
            run.id,
            NormalizedJob(
                source="test",
                company_id="acme",
                external_job_id="2",
                title="Python Intern",
                location="Mumbai, India",
                apply_url="https://example.com/2",
            ),
        )
        session.add_all(
            [
                JobMatchScore(
                    job_id=strong.id,
                    score=88,
                    application_priority=91,
                    eligibility_status="eligible",
                    rejected=False,
                    matched_skills=["python"],
                    reasons=["Strong match"],
                ),
                JobMatchScore(
                    job_id=rejected.id,
                    score=0,
                    application_priority=0,
                    eligibility_status="ineligible",
                    rejected=True,
                ),
            ]
        )
        session.commit()

        output = tmp_path / "application-queue.csv"
        count = prepare_application_queue(session, output)

        assert count == 1
        application = session.scalar(select(Application))
        assert application.job_id == strong.id
        assert application.status == "ready_for_review"
        content = output.read_text()
        assert "Senior Python Backend Engineer" in content
        assert "Python Intern" not in content
