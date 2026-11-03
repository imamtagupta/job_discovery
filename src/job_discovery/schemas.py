from datetime import datetime
from enum import StrEnum

from pydantic import BaseModel, Field, HttpUrl


class WorkAuthorization(StrEnum):
    CONFIRMED_ELIGIBLE = "confirmed_eligible"
    CONFIRMED_INELIGIBLE = "confirmed_ineligible"
    SPONSORSHIP_AVAILABLE = "sponsorship_available"
    UNKNOWN = "unknown"


class Availability(StrEnum):
    ACTIVE = "active"
    MISSING = "missing"
    CLOSED = "closed"


class NormalizedJob(BaseModel):
    source: str
    company_id: str
    external_job_id: str
    title: str
    location: str | None = None
    description_html: str | None = None
    description_text: str = ""
    apply_url: HttpUrl
    canonical_url: HttpUrl | None = None
    requisition_id: str | None = None
    employment_type: str | None = None
    salary_min: int | None = None
    salary_max: int | None = None
    salary_currency: str | None = None
    source_posted_at: datetime | None = None
    source_updated_at: datetime | None = None
    work_authorization: WorkAuthorization = WorkAuthorization.UNKNOWN
    raw: dict = Field(default_factory=dict)


class ScoreResult(BaseModel):
    score: float = Field(ge=0, le=100)
    role_family: str = "Other"
    primary_language: str = "unknown"
    eligibility_status: str = "needs_review"
    experience_fit: str = "unknown"
    location_eligibility: str = "unknown"
    compensation_evidence: str = "unknown"
    visa_eligibility_evidence: str = "unknown"
    data_confidence: str = "low"
    resume_relevance: float = Field(default=0, ge=0, le=100)
    application_eligibility: float = Field(default=0, ge=0, le=100)
    posting_freshness: float = Field(default=0, ge=0, le=100)
    company_preference: float = Field(default=0, ge=0, le=100)
    application_priority: float = Field(default=0, ge=0, le=100)
    rejected: bool = False
    matched_skills: list[str] = Field(default_factory=list)
    missing_skills: list[str] = Field(default_factory=list)
    matched_mandatory_skills: list[str] = Field(default_factory=list)
    missing_mandatory_skills: list[str] = Field(default_factory=list)
    transferable_skills: list[str] = Field(default_factory=list)
    breakdown: dict[str, float] = Field(default_factory=dict)
    reasons: list[str] = Field(default_factory=list)
