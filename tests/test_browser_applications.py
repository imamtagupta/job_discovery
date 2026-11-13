import csv
from pathlib import Path

from job_discovery.services.browser_applications import (
    _browser_profile_key,
    _choice_matches,
    _is_selectable_prompt_option,
    _requires_nested_choice,
    _selection_instruction_committed,
    _workday_multiselect_container,
    _workday_step,
    answer_for_question,
    load_discovered_jobs,
    load_queue_job,
)


class _TextLocator:
    def __init__(self, text: str):
        self.text = text

    def inner_text(self):
        return self.text


class _PageWithBody:
    def __init__(self, text: str):
        self.text = text

    def locator(self, selector: str):
        assert selector == "body"
        return _TextLocator(self.text)


class _CountLocator:
    def __init__(self, count: int = 1):
        self._count = count

    def count(self):
        return self._count


class _MultiselectInput:
    def get_attribute(self, name: str):
        assert name == "data-uxi-multiselect-id"
        return "source-widget"


class _MultiselectScope:
    def __init__(self):
        self.selector = None

    def locator(self, selector: str):
        self.selector = selector
        return _CountLocator()


class _PromptOption:
    def __init__(self, *, role: str = "", disabled: str | None = None, inputs: int = 0):
        self.attributes = {"role": role, "aria-disabled": disabled}
        self.inputs = inputs

    def get_attribute(self, name: str):
        return self.attributes.get(name)

    def locator(self, selector: str):
        return _CountLocator(self.inputs)


def test_workday_choice_matching_is_scoped_and_semantic():
    assert _choice_matches("company careers page", "career site")
    assert _choice_matches("career site", "career site")
    assert _choice_matches("no", "i am not a protected veteran")
    assert not _choice_matches("company careers page", "india 91")
    assert _requires_nested_choice("company careers page")
    assert _requires_nested_choice("career site")
    assert not _requires_nested_choice("india 91")
    assert _selection_instruction_committed("1 item selected, Career Site", "Career Site")
    assert not _selection_instruction_committed("0 items selected", "Career Site")


def test_workday_multiselect_is_scoped_by_widget_id():
    scope = _MultiselectScope()
    assert _workday_multiselect_container(scope, _MultiselectInput()) is not None
    assert scope.selector == (
        '[data-automation-id="multiSelectContainer"][id="source-widget"]'
    )


def test_workday_nested_choice_rejects_heading_with_same_text():
    assert not _is_selectable_prompt_option(_PromptOption())
    assert not _is_selectable_prompt_option(_PromptOption(role="option", disabled="true"))
    assert _is_selectable_prompt_option(_PromptOption(role="option"))
    assert _is_selectable_prompt_option(_PromptOption(inputs=1))


def test_workday_step_reads_progress_state():
    page = _PageWithBody("completed step 1 of 6 current step 2 of 6 My Information")
    assert _workday_step(page) == 2


def test_browser_profile_key_is_stable_per_employer():
    assert _browser_profile_key(
        {"company": "Palo Alto Networks", "ats_provider": "workday"}
    ) == "workday-palo-alto-networks"


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


def test_answer_for_question_matches_required_country_and_city_labels():
    profile = {
        "candidate": {"location": "Mumbai, India"},
        "application": {"country": "India"},
    }
    faq = Path("config/application_faq.yaml")
    assert answer_for_question(
        "Country*", profile=profile, faq_path=faq, location="New York City"
    ) == ("approved", "India")
    assert answer_for_question(
        "Location (City)*", profile=profile, faq_path=faq, location="New York City"
    ) == ("approved", "Mumbai")


def test_answer_for_question_matches_workday_label_variants():
    profile = {
        "application": {
            "first_name": "Mamta",
            "answers": {
                "source": {
                    "question": "How did you hear about this job?",
                    "aliases": ["How Did You Hear About Us?"],
                    "answer": "Company careers page",
                    "status": "approved",
                }
            },
        }
    }
    faq = Path("config/application_faq.yaml")
    assert answer_for_question(
        "Given Name(s)*", profile=profile, faq_path=faq, location="India"
    ) == ("approved", "Mamta")
    assert answer_for_question(
        "How Did You Hear About Us?*", profile=profile, faq_path=faq, location="India"
    ) == ("approved", "Company careers page")


def test_answer_for_question_matches_local_name_variants():
    profile = {
        "application": {
            "first_name": "Mamta",
            "last_name": "Gupta",
            "preferred_name": "Mamta",
        }
    }
    faq = Path("config/application_faq.yaml")
    assert answer_for_question(
        "Local Given Name(s)", profile=profile, faq_path=faq, location="India"
    ) == ("approved", "Mamta")
    assert answer_for_question(
        "Local Family Name", profile=profile, faq_path=faq, location="India"
    ) == ("approved", "Gupta")


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
