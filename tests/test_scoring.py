from job_discovery.models import Job
from job_discovery.services.scoring import score_job


def make_job(**values):
    defaults = dict(
        id=1,
        stable_id="x",
        source="test",
        company_id="acme",
        external_job_id="1",
        title="Senior Python Backend Engineer",
        normalized_title="senior python backend engineer",
        location="Mumbai, India",
        normalized_location="mumbai india",
        description_text=(
            "Build distributed APIs using Python FastAPI PostgreSQL AWS Docker. "
            "5+ years experience."
        ),
        description_hash="x",
        apply_url="https://example.com",
        availability="active",
        work_authorization="unknown",
        raw={},
    )
    defaults.update(values)
    return Job(**defaults)


def test_strong_role_scores_and_unknowns_remain_visible(profile, scoring_config):
    result = score_job(make_job(), profile, scoring_config)
    assert result.score >= 70
    assert "python" in result.matched_skills
    assert any("Salary undisclosed" in reason for reason in result.reasons)
    assert any("Work authorization unknown" in reason for reason in result.reasons)


def test_hard_filter_rejects_intern(profile, scoring_config):
    result = score_job(make_job(title="Python Intern"), profile, scoring_config)
    assert result.rejected
    assert result.score == 0


def test_java_only_mandatory_role_is_rejected(profile, scoring_config):
    job = make_job(
        title="Senior Java Backend Engineer",
        description_text="Requirements: Java and Spring Boot are required. Build backend services.",
    )
    result = score_job(job, profile, scoring_config)
    assert result.rejected
    assert result.primary_language == "java"
    assert any("unsupported mandatory" in reason for reason in result.reasons)


def test_generic_remote_requires_review(profile, scoring_config):
    job = make_job(location="Remote", normalized_location="remote")
    result = score_job(job, profile, scoring_config)
    assert result.location_eligibility == "unknown"
    assert result.eligibility_status == "needs_review"


def test_unknown_salary_is_not_rejected(profile, scoring_config):
    result = score_job(make_job(salary_min=None, salary_max=None), profile, scoring_config)
    assert not result.rejected
    assert result.compensation_evidence == "unknown"


def test_ai_agent_backend_role_uses_resume_achievement(profile, scoring_config):
    job = make_job(
        title="Senior AI Application Engineer",
        description_text=(
            "Build production Python APIs and AI agent orchestration on AWS. "
            "Use CloudWatch for automated diagnostics. Requires 5+ years experience."
        ),
    )
    result = score_job(job, profile, scoring_config)
    assert result.role_family == "AI Application Engineering"
    assert "ai_diagnostics" in result.transferable_skills
    assert result.resume_relevance >= 70
