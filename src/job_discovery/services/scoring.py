import re
from datetime import UTC, datetime
from typing import Any

from ..models import Job
from ..schemas import ScoreResult


def _contains(text: str, term: str) -> bool:
    return bool(re.search(rf"(?<!\w){re.escape(term.lower())}(?!\w)", text.lower()))


def _skills(profile: dict[str, Any]) -> dict[str, list[str]]:
    return {
        name: aliases for tier in profile["skill_tiers"].values() for name, aliases in tier.items()
    }


def _sentences(text: str) -> list[str]:
    return [part.strip().lower() for part in re.split(r"[\n.!?;]+", text) if part.strip()]


def classify_role(title: str, description: str, profile: dict[str, Any]) -> str:
    title, text = title.lower(), f"{title} {description}".lower()
    if "full stack" in title or "full-stack" in title:
        return "Full Stack"
    if "python" in title and any(word in title for word in ("backend", "software", "engineer")):
        return "Python Backend"
    scores = {
        family: sum(
            3 if _contains(title, alias) else 1 for alias in aliases if _contains(text, alias)
        )
        for family, aliases in profile["role_families"].items()
    }
    return max(scores, key=scores.get) if max(scores.values(), default=0) else "Other"


def _primary_language(title: str, description: str, profile: dict[str, Any]) -> str:
    languages = {"python": ["python"], "javascript": ["javascript", "node.js", "nodejs"]}
    languages.update(profile.get("unsupported_primary_languages", {}))
    for name, aliases in languages.items():
        if any(_contains(title, alias) for alias in aliases):
            return name
    counts = {
        name: sum(_contains(description, alias) for alias in aliases)
        for name, aliases in languages.items()
    }
    return max(counts, key=counts.get) if max(counts.values(), default=0) else "unknown"


def _experience(text: str, candidate_years: int) -> tuple[str, list[int]]:
    ranges = [
        (int(a), int(b)) for a, b in re.findall(r"(\d+)\s*[-–]\s*(\d+)\s*(?:years|yrs)", text)
    ]
    values = [int(value) for value in re.findall(r"(\d+)\+?\s*(?:years|yrs)", text)]
    requirements = [pair[0] for pair in ranges] + values
    if not requirements:
        return "unknown", []
    minimum = min(requirements)
    if minimum > candidate_years + 2:
        return "incompatible", requirements
    return ("fit" if minimum <= candidate_years else "stretch"), requirements


def _location(job: Job, description: str, profile: dict[str, Any]) -> tuple[str, float, str]:
    location = (job.location or "").lower()
    text = f"{location} {description.lower()}"
    if any(term in text for term in ("remote india", "india remote", "anywhere in india")):
        return "confirmed_eligible", 9.5, "Explicitly remote within India"
    for item in profile["location_priority"]:
        if any(alias in location for alias in item["aliases"]):
            return "confirmed_eligible", item["score"] / 10, item["label"]
    if "india" in location:
        return "confirmed_eligible", 7, "India location"
    if "remote" in text:
        return "unknown", 3, "Generic remote; India eligibility not stated"
    return "unknown", 1 if location else 2, "Location or relocation eligibility requires review"


def _freshness(job: Job) -> tuple[float, str]:
    if not job.source_posted_at:
        return 0, "Source posting date unavailable; first discovery is not posting date"
    posted = job.source_posted_at
    if posted.tzinfo is None:
        posted = posted.replace(tzinfo=UTC)
    days = max((datetime.now(UTC) - posted).days, 0)
    points = 5 if days <= 7 else 4 if days <= 30 else 2 if days <= 60 else 0
    return points, f"Verified source posting date is {days} day(s) old"


def score_job(job: Job, profile: dict[str, Any], config: dict[str, Any]) -> ScoreResult:
    title, description = job.title.strip().lower(), job.description_text or ""
    text = f"{title} {description.lower()}"
    hard = config["hard_filters"]
    rejection = [term for term in hard["excluded_title_terms"] if _contains(title, term)]
    if job.employment_type and job.employment_type.lower() in hard["excluded_employment_types"]:
        rejection.append(f"employment type: {job.employment_type}")

    skills = _skills(profile)
    matched = sorted(
        name for name, aliases in skills.items() if any(_contains(text, a) for a in aliases)
    )
    required_text = " ".join(
        s for s in _sentences(description) if any(m in s for m in config["mandatory_markers"])
    )
    mandatory = {
        name
        for name, aliases in skills.items()
        if any(_contains(required_text, a) for a in aliases)
    }
    unsupported_required = [
        name
        for name, aliases in profile["unsupported_primary_languages"].items()
        if required_text and any(_contains(required_text, alias) for alias in aliases)
    ]
    if unsupported_required and not _contains(required_text, "python"):
        rejection.append(f"unsupported mandatory primary language: {unsupported_required[0]}")

    years = profile["candidate"]["years_experience"]
    experience_fit, experience_values = _experience(description.lower(), years)
    if experience_fit == "incompatible":
        rejection.append("experience requirement substantially exceeds 6 years")
    overleveled = [term for term in hard["overleveled_title_terms"] if _contains(title, term)]
    if overleveled:
        rejection.append(f"role scope likely over-levelled: {overleveled[0]}")

    family = classify_role(title, description, profile)
    language = _primary_language(title, description, profile)
    backend = any(
        _contains(text, word)
        for word in ("backend", "api", "apis", "microservices", "distributed systems")
    )
    python = _contains(text, "python")
    breakdown: dict[str, float] = {}
    reasons: list[str] = []
    breakdown["primary_alignment"] = (
        30
        if python and backend
        else 20
        if python or family in {"Backend / Distributed Systems", "Platform Engineering"}
        else 13
        if family == "Full Stack" and backend
        else 3
    )
    if python and backend:
        reasons.append("Python is paired with backend/API/distributed-systems ownership")
    tier1 = profile["skill_tiers"]["tier_1"]
    tier1_hits = [name for name in tier1 if name in matched]
    architecture = 20 * min(len(tier1_hits), 5) / 5
    matched_mandatory = sorted(mandatory.intersection(matched))
    if mandatory:
        architecture *= 0.5 + 0.5 * len(matched_mandatory) / len(mandatory)
    breakdown["architecture_skills"] = round(architecture, 2)
    breakdown["experience_seniority"] = {"fit": 15, "stretch": 8, "unknown": 9, "incompatible": 0}[
        experience_fit
    ]
    reasons.append(f"Experience fit: {experience_fit}")
    achievement_hits = [
        item for item in profile["achievements"] if any(_contains(text, a) for a in item["aliases"])
    ]
    breakdown["domain_achievements"] = min(len(achievement_hits), 3) * 5
    reasons.extend(f"Transferable achievement: {item['evidence']}" for item in achievement_hits[:3])
    location_status, location_points, location_reason = _location(job, description, profile)
    breakdown["location_eligibility"] = location_points
    reasons.append(location_reason)
    if job.work_authorization == "confirmed_ineligible":
        rejection.append("confirmed work-authorization mismatch")
    elif job.work_authorization == "unknown":
        reasons.append("Work authorization unknown")
    if job.salary_min is None and job.salary_max is None:
        compensation = "unknown"
        breakdown["compensation_evidence"] = 0
        reasons.append("Salary undisclosed; retained for review")
    else:
        compensation = (
            f"{job.salary_min or '?'}-{job.salary_max or '?'} {job.salary_currency or ''}".strip()
        )
        threshold = profile["compensation"]["india_min_lpa"] * 100_000
        breakdown["compensation_evidence"] = 5 if (job.salary_max or 0) >= threshold else 1
    fresh_points, fresh_reason = _freshness(job)
    breakdown["freshness"] = fresh_points
    reasons.append(fresh_reason)

    score = min(round(sum(breakdown.values()), 2), 100)
    rejected = bool(rejection)
    if rejected:
        score = 0
        reasons = [f"Hard eligibility: {item}" for item in rejection] + reasons
    eligibility = 0 if rejected else 100 if location_status == "confirmed_eligible" else 60
    relevant = (
        "primary_alignment",
        "architecture_skills",
        "experience_seniority",
        "domain_achievements",
    )
    resume_relevance = round(100 * sum(breakdown[key] for key in relevant) / 80, 2)
    posting_freshness = fresh_points * 20
    product_company = bool(job.company and job.company.product_company)
    company_preferences = config.get("company_preferences", {})
    company_preference = float(
        company_preferences.get(
            "product_company_priority" if product_company else "other_company_priority",
            100 if product_company else 50,
        )
    )
    if product_company:
        reasons.append("Product-based company preference applied")
    priority_weights = config.get(
        "application_priority_weights",
        {
            "resume_relevance": 0.60,
            "eligibility": 0.15,
            "freshness": 0.10,
            "company_preference": 0.15,
        },
    )
    application_priority = (
        0
        if rejected
        else round(
            priority_weights["resume_relevance"] * resume_relevance
            + priority_weights["eligibility"] * eligibility
            + priority_weights["freshness"] * posting_freshness
            + priority_weights["company_preference"] * company_preference,
            2,
        )
    )
    signals = sum(
        (bool(description), bool(job.location), bool(experience_values), bool(job.source_posted_at))
    )
    confidence = "high" if signals >= 3 else "medium" if signals >= 2 else "low"
    eligibility_status = (
        "ineligible"
        if rejected
        else "eligible"
        if location_status == "confirmed_eligible"
        else "needs_review"
    )
    return ScoreResult(
        score=score,
        role_family=family,
        primary_language=language,
        eligibility_status=eligibility_status,
        experience_fit=experience_fit,
        location_eligibility=location_status,
        compensation_evidence=compensation,
        visa_eligibility_evidence=job.work_authorization,
        data_confidence=confidence,
        resume_relevance=resume_relevance,
        application_eligibility=eligibility,
        posting_freshness=posting_freshness,
        company_preference=company_preference,
        application_priority=application_priority,
        rejected=rejected,
        matched_skills=matched,
        missing_skills=sorted(set(skills) - set(matched)),
        matched_mandatory_skills=matched_mandatory,
        missing_mandatory_skills=unsupported_required,
        transferable_skills=[item["theme"] for item in achievement_hits],
        breakdown=breakdown,
        reasons=reasons,
    )
