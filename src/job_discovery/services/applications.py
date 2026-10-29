import hashlib
import json
import re
from datetime import date
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, Field, model_validator

AnswerMode = Literal["auto_fill", "review_once", "always_review"]
RiskLevel = Literal["low", "medium", "high"]


class KnowledgeEntry(BaseModel):
    key: str = Field(pattern=r"^[a-z0-9][a-z0-9_.-]*$")
    question: str
    aliases: list[str] = Field(default_factory=list)
    answer: Any | None = None
    answer_mode: AnswerMode = "review_once"
    risk_level: RiskLevel = "medium"
    autonomous_use: bool = False
    evidence: str
    countries: list[str] = Field(default_factory=lambda: ["*"])
    employers: list[str] = Field(default_factory=lambda: ["*"])
    reviewed_at: date | None = None
    expires_at: date | None = None
    enabled: bool = True

    @model_validator(mode="after")
    def validate_autonomous_use(self) -> "KnowledgeEntry":
        if self.autonomous_use and self.answer_mode != "auto_fill":
            raise ValueError("autonomous_use requires answer_mode=auto_fill")
        if self.autonomous_use and self.risk_level == "high":
            raise ValueError("high-risk answers cannot be autonomous")
        return self


class ApplicationKnowledgeBase(BaseModel):
    version: int = 1
    entries: list[KnowledgeEntry]

    @model_validator(mode="after")
    def unique_keys(self) -> "ApplicationKnowledgeBase":
        keys = [entry.key for entry in self.entries]
        if len(keys) != len(set(keys)):
            raise ValueError("knowledge-base entry keys must be unique")
        return self


class FormQuestion(BaseModel):
    id: str
    label: str
    required: bool = False
    field_type: str = "text"
    options: list[str] = Field(default_factory=list)


class ApplicationForm(BaseModel):
    job_stable_id: str | None = None
    company: str | None = None
    country: str | None = None
    source_url: str | None = None
    questions: list[FormQuestion]


class ProposedAnswer(BaseModel):
    question_id: str
    question: str
    status: Literal["auto_filled", "needs_review", "unanswered"]
    answer: Any | None = None
    knowledge_key: str | None = None
    confidence: float = 0
    reason: str
    risk_level: RiskLevel | None = None
    evidence: str | None = None
    answer_fingerprint: str | None = None


def load_knowledge_base(path: Path) -> ApplicationKnowledgeBase:
    with path.open(encoding="utf-8") as stream:
        return ApplicationKnowledgeBase.model_validate(yaml.safe_load(stream) or {})


def load_application_form(path: Path) -> ApplicationForm:
    with path.open(encoding="utf-8") as stream:
        return ApplicationForm.model_validate(yaml.safe_load(stream) or {})


def _tokens(value: str) -> set[str]:
    normalized = re.sub(r"[^a-z0-9]+", " ", value.lower())
    ignored = {"a", "an", "and", "are", "do", "for", "is", "of", "the", "to", "you", "your"}
    return {token for token in normalized.split() if token not in ignored}


def _similarity(left: str, right: str) -> float:
    left_tokens = _tokens(left)
    right_tokens = _tokens(right)
    if not left_tokens or not right_tokens:
        return 0
    if left_tokens == right_tokens:
        return 1
    intersection = len(left_tokens & right_tokens)
    return intersection / len(left_tokens | right_tokens)


def _applies(entry: KnowledgeEntry, company: str | None, country: str | None) -> bool:
    employers = {item.casefold() for item in entry.employers}
    countries = {item.casefold() for item in entry.countries}
    company_ok = "*" in employers or bool(company and company.casefold() in employers)
    country_ok = "*" in countries or bool(country and country.casefold() in countries)
    return company_ok and country_ok


def _fingerprint(entry: KnowledgeEntry) -> str:
    payload = json.dumps(
        {"key": entry.key, "answer": entry.answer, "reviewed_at": str(entry.reviewed_at)},
        sort_keys=True,
        default=str,
    )
    return hashlib.sha256(payload.encode()).hexdigest()


def answer_question(
    question: FormQuestion,
    knowledge_base: ApplicationKnowledgeBase,
    *,
    company: str | None = None,
    country: str | None = None,
    today: date | None = None,
) -> ProposedAnswer:
    today = today or date.today()
    candidates: list[tuple[float, KnowledgeEntry]] = []
    for entry in knowledge_base.entries:
        if not entry.enabled or not _applies(entry, company, country):
            continue
        score = max(
            _similarity(question.label, phrase) for phrase in [entry.question, *entry.aliases]
        )
        if score >= 0.60:
            candidates.append((score, entry))
    candidates.sort(key=lambda item: (-item[0], item[1].key))
    if not candidates:
        return ProposedAnswer(
            question_id=question.id,
            question=question.label,
            status="unanswered",
            reason="No sufficiently similar approved knowledge-base entry.",
        )
    score, entry = candidates[0]
    if len(candidates) > 1 and candidates[1][0] == score:
        return ProposedAnswer(
            question_id=question.id,
            question=question.label,
            status="needs_review",
            confidence=score,
            reason="Multiple knowledge-base entries matched equally; no answer was selected.",
        )
    if entry.answer is None:
        return ProposedAnswer(
            question_id=question.id,
            question=question.label,
            status="unanswered",
            knowledge_key=entry.key,
            confidence=round(score, 3),
            reason="A matching policy entry exists, but it has no candidate-approved answer.",
            risk_level=entry.risk_level,
            evidence=entry.evidence,
        )
    expired = entry.expires_at is not None and entry.expires_at < today
    autonomous = (
        entry.answer_mode == "auto_fill"
        and entry.autonomous_use
        and not expired
        and score >= 0.75
    )
    reason = "Approved low-risk answer matched."
    if expired:
        reason = "The approved answer has expired and must be reviewed."
    elif entry.answer_mode != "auto_fill" or not entry.autonomous_use:
        reason = f"Entry policy is {entry.answer_mode}; review is required."
    elif score < 0.75:
        reason = "The question match is plausible but below the autonomous threshold."
    return ProposedAnswer(
        question_id=question.id,
        question=question.label,
        status="auto_filled" if autonomous else "needs_review",
        answer=entry.answer,
        knowledge_key=entry.key,
        confidence=round(score, 3),
        reason=reason,
        risk_level=entry.risk_level,
        evidence=entry.evidence,
        answer_fingerprint=_fingerprint(entry),
    )


def prepare_application(
    form: ApplicationForm, knowledge_base: ApplicationKnowledgeBase
) -> list[ProposedAnswer]:
    return [
        answer_question(
            question,
            knowledge_base,
            company=form.company,
            country=form.country,
        )
        for question in form.questions
    ]

