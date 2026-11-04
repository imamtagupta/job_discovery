from pathlib import Path

import yaml

from job_discovery.services.application_faq import (
    add_faq_entry,
    record_unseen_question,
    resolve_question,
)


def test_resolve_known_alias_and_leave_sensitive_answer_for_review(tmp_path):
    path = tmp_path / "faq.yaml"
    path.write_text(
        yaml.safe_dump(
            {
                "entries": [
                    {
                        "key": "visa",
                        "question": "Will you require visa sponsorship?",
                        "aliases": ["Do you need sponsorship?"],
                        "answer": None,
                        "status": "needs_review",
                    }
                ]
            }
        )
    )
    result = resolve_question(path, "Do you need sponsorship?")
    assert result["key"] == "visa"
    assert result["status"] == "needs_review"
    assert result["answer"] is None


def test_unseen_question_is_recorded_once(tmp_path):
    backlog = tmp_path / "backlog.yaml"
    assert record_unseen_question(backlog, "Can you travel 25%?", "acme")
    assert not record_unseen_question(backlog, "Can you travel 25%?", "other")
    document = yaml.safe_load(backlog.read_text())
    assert len(document["questions"]) == 1


def test_add_approved_faq_entry(tmp_path):
    path = tmp_path / "faq.yaml"
    path.write_text("version: 1\nentries: []\n")
    add_faq_entry(
        path,
        key="travel",
        question="Can you travel?",
        answer="Yes, up to 10%",
        aliases=["Are you willing to travel?"],
    )
    result = resolve_question(path, "Are you willing to travel?")
    assert result["status"] == "approved"
    assert result["answer"] == "Yes, up to 10%"


def test_sponsorship_answer_is_conditional_by_location():
    result = resolve_question(
        Path("config/application_faq.yaml"),
        "Do you require employment sponsorship?",
    )
    assert result["status"] == "approved"
    assert result["answer"] == {"india": "No", "outside_india": "Yes"}
