import csv
from pathlib import Path

from job_discovery.services.browser_applications import (
    answer_for_question,
    load_discovered_jobs,
    load_queue_job,
)


def test_load_queue_job_selects_only_ready_rows(tmp_path):
    queue = tmp_path / "queue.csv"
    with queue.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=["stable_job_id", "status", "company", "title", "direct_apply_url"],
        )
        writer.writeheader()
        writer.writerow(
            {
                "stable_job_id": "skip",
                "status": "needs_input",
                "company": "A",
                "title": "A",
                "direct_apply_url": "https://example.com/a",
            }
        )
        writer.writerow(
            {
                "stable_job_id": "ready",
                "status": "ready_for_review",
                "company": "B",
                "title": "B",
                "direct_apply_url": "https://example.com/b",
            }
        )
    assert load_queue_job(queue)["stable_job_id"] == "ready"


def test_answer_for_question_uses_profile_and_location_aware_faq():
    profile = {
        "candidate": {"name": "Mamta Gupta", "location": "Mumbai, India"},
        "application": {"first_name": "Mamta", "email": "mamta@example.com"},
    }
    faq = Path("config/application_faq.yaml")
    assert answer_for_question("First name", profile=profile, faq_path=faq, location="India") == (
        "approved",
        "Mamta",
    )
    assert answer_for_question(
        "Do you require employment sponsorship?",
        profile=profile,
        faq_path=faq,
        location="Pune, India",
    ) == ("approved", "No")
    assert answer_for_question(
        "Do you require employment sponsorship?",
        profile=profile,
        faq_path=faq,
        location="London, UK",
    ) == ("approved", "Yes")


def test_answer_for_question_uses_profile_application_answers():
    profile = {
        "application": {
            "answers": {
                "source": {
                    "question": "How did you hear about this job?",
                    "aliases": ["How did you learn about this job?"],
                    "answer": "Company careers page",
                    "status": "approved",
                }
            }
        }
    }
    assert answer_for_question(
        "How did you learn about this job?",
        profile=profile,
        faq_path=Path("config/application_faq.yaml"),
        location="India",
    ) == ("approved", "Company careers page")


def test_load_discovered_jobs_prioritizes_report_and_filters_ineligible(tmp_path):
    report = tmp_path / "report.md"
    report.write_text("- Apply: https://example.com/recommended\n", encoding="utf-8")
    jobs = tmp_path / "jobs.csv"
    with jobs.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(
            handle,
            fieldnames=[
                "stable_job_id",
                "eligibility_status",
                "application_status",
                "application_priority",
                "match_score",
                "direct_apply_url",
            ],
        )
        writer.writeheader()
        writer.writerows(
            [
                {
                    "stable_job_id": "remaining",
                    "eligibility_status": "eligible",
                    "application_status": "not_applied",
                    "application_priority": "99",
                    "match_score": "99",
                    "direct_apply_url": "https://example.com/remaining",
                },
                {
                    "stable_job_id": "recommended",
                    "eligibility_status": "eligible",
                    "application_status": "not_applied",
                    "application_priority": "80",
                    "match_score": "80",
                    "direct_apply_url": "https://example.com/recommended",
                },
                {
                    "stable_job_id": "blocked",
                    "eligibility_status": "ineligible",
                    "application_status": "not_applied",
                    "application_priority": "100",
                    "match_score": "100",
                    "direct_apply_url": "https://example.com/blocked",
                },
                {
                    "stable_job_id": "review",
                    "eligibility_status": "needs_review",
                    "application_status": "not_applied",
                    "application_priority": "90",
                    "match_score": "90",
                    "direct_apply_url": "https://example.com/review",
                },
                {
                    "stable_job_id": "attempted",
                    "eligibility_status": "eligible",
                    "application_status": "submission_attempted",
                    "application_priority": "70",
                    "match_score": "70",
                    "direct_apply_url": "https://example.com/attempted",
                },
                {
                    "stable_job_id": "submitted",
                    "eligibility_status": "eligible",
                    "application_status": "submitted",
                    "application_priority": "100",
                    "match_score": "100",
                    "direct_apply_url": "https://example.com/submitted",
                },
            ]
        )

    assert [row["stable_job_id"] for row in load_discovered_jobs(report, jobs)] == [
        "recommended",
        "remaining",
    ]
    assert [
        row["stable_job_id"]
        for row in load_discovered_jobs(report, jobs, include_needs_review=True)
    ] == ["recommended", "remaining", "review"]
    assert [
        row["stable_job_id"]
        for row in load_discovered_jobs(
            report, jobs, include_needs_review=True, retry_incomplete=True
        )
    ] == ["recommended", "remaining", "review", "attempted"]
