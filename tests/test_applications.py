from datetime import date

import pytest
from pydantic import ValidationError

from job_discovery.services.applications import (
    ApplicationForm,
    ApplicationKnowledgeBase,
    FormQuestion,
    KnowledgeEntry,
    answer_question,
    prepare_application,
)


def entry(**overrides):
    values = {
        "key": "profile.location",
        "question": "What is your current location?",
        "aliases": ["Where are you currently located?"],
        "answer": "Mumbai, India",
        "answer_mode": "auto_fill",
        "risk_level": "low",
        "autonomous_use": True,
        "evidence": "Resume candidate profile",
        "reviewed_at": date(2026, 10, 29),
    }
    values.update(overrides)
    return KnowledgeEntry.model_validate(values)


def test_exact_alias_is_autofilled_with_evidence():
    result = answer_question(
        FormQuestion(id="location", label="Where are you currently located?", required=True),
        ApplicationKnowledgeBase(entries=[entry()]),
    )

    assert result.status == "auto_filled"
    assert result.answer == "Mumbai, India"
    assert result.confidence == 1
    assert result.evidence == "Resume candidate profile"
    assert result.answer_fingerprint


def test_high_risk_answer_always_requires_review():
    sponsorship = entry(
        key="authorization.sponsorship",
        question="Will you require visa sponsorship?",
        aliases=["Do you require sponsorship?"],
        answer="Yes",
        answer_mode="always_review",
        risk_level="high",
        autonomous_use=False,
        evidence="Candidate confirmation",
    )
    result = answer_question(
        FormQuestion(id="sponsor", label="Do you require sponsorship?"),
        ApplicationKnowledgeBase(entries=[sponsorship]),
    )

    assert result.status == "needs_review"
    assert result.answer == "Yes"


def test_unknown_question_is_not_guessed():
    result = answer_question(
        FormQuestion(id="criminal", label="Have you ever been convicted of an offence?"),
        ApplicationKnowledgeBase(entries=[entry()]),
    )

    assert result.status == "unanswered"
    assert result.answer is None


def test_known_sensitive_question_without_approved_answer_stays_unanswered():
    sponsorship = entry(
        key="authorization.sponsorship",
        question="Will you require visa sponsorship?",
        answer=None,
        answer_mode="always_review",
        risk_level="high",
        autonomous_use=False,
        evidence="Candidate confirmation required",
    )
    result = answer_question(
        FormQuestion(id="sponsor", label="Will you require visa sponsorship?"),
        ApplicationKnowledgeBase(entries=[sponsorship]),
    )

    assert result.status == "unanswered"
    assert result.knowledge_key == "authorization.sponsorship"
    assert result.risk_level == "high"
    assert "no candidate-approved answer" in result.reason


def test_expired_answer_requires_review():
    result = answer_question(
        FormQuestion(id="location", label="Where are you currently located?"),
        ApplicationKnowledgeBase(entries=[entry(expires_at=date(2026, 1, 1))]),
        today=date(2026, 10, 29),
    )

    assert result.status == "needs_review"
    assert "expired" in result.reason


def test_country_scope_prevents_wrong_authorization_answer():
    india = entry(countries=["India"])
    result = answer_question(
        FormQuestion(id="location", label="Where are you currently located?"),
        ApplicationKnowledgeBase(entries=[india]),
        country="United States",
    )

    assert result.status == "unanswered"


def test_high_risk_entry_cannot_be_autonomous():
    with pytest.raises(ValidationError):
        entry(risk_level="high")


def test_prepare_application_preserves_separate_question_results():
    form = ApplicationForm(
        company="Acme",
        country="India",
        questions=[
            FormQuestion(id="location", label="Where are you currently located?"),
            FormQuestion(id="unknown", label="What is your favourite editor?"),
        ],
    )
    results = prepare_application(form, ApplicationKnowledgeBase(entries=[entry()]))

    assert [result.status for result in results] == ["auto_filled", "unanswered"]

