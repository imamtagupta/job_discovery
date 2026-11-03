from typing import Any

import gspread
from sqlalchemy import select
from sqlalchemy.orm import Session

from ..models import Application, AtsSource, Company, Job, JobMatchScore

HEADERS = [
    "Stable Job ID",
    "Company",
    "Job Title",
    "Role Family",
    "Primary Language",
    "Required Experience",
    "Location",
    "Remote Eligibility",
    "Compensation",
    "Visa Sponsorship",
    "Resume Match Score",
    "Eligibility Status",
    "Matched Skills",
    "Missing Skills",
    "Ranking Explanation",
    "Posting Date",
    "First Discovered",
    "Direct Application URL",
    "Recruiter / Contact",
    "Company ATS Source",
    "Application Status",
    "Notes",
    "Application Priority",
]
STATUS_COLUMN = 20
NOTES_COLUMN = 21


def safe_cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(("=", "+", "-", "@")):
        return "'" + value
    return value


class SheetsSync:
    def __init__(self, spreadsheet_id: str, credentials_json: str):
        self.book = gspread.service_account(filename=credentials_json).open_by_key(spreadsheet_id)

    def _worksheet(self, name: str):
        try:
            return self.book.worksheet(name)
        except gspread.WorksheetNotFound:
            return self.book.add_worksheet(name, rows=1000, cols=len(HEADERS))

    def _existing(self, worksheet) -> dict[str, tuple[int, list[str]]]:
        values = worksheet.get_all_values()
        return {row[0]: (index, row) for index, row in enumerate(values[1:], start=2) if row}

    def sync(self, session: Session, threshold: float = 80) -> None:
        applications = {item.job_id: item for item in session.scalars(select(Application))}
        scores = {item.job_id: item for item in session.scalars(select(JobMatchScore))}
        companies = {item.id: item.name for item in session.scalars(select(Company))}
        product_company_ids = {
            item.id
            for item in session.scalars(select(Company).where(Company.product_company.is_(True)))
        }
        jobs = list(session.scalars(select(Job).order_by(Job.first_seen_at.desc())))
        rows = {job.id: self._row(job, companies, scores, applications) for job in jobs}
        active = [
            (job, scores.get(job.id), rows[job.id]) for job in jobs if job.availability == "active"
        ]
        views = {
            "All Jobs": list(rows.values()),
            "High Priority": [
                row
                for _, score, row in active
                if score
                and not score.rejected
                and score.score >= threshold
                and score.eligibility_status == "eligible"
            ],
            "Python Backend": [
                row
                for _, score, row in active
                if score and score.role_family == "Python Backend" and not score.rejected
            ],
            "Product Companies": [
                row
                for job, score, row in active
                if job.company_id in product_company_ids and score and not score.rejected
            ],
            "Fintech - Quant": [
                row
                for _, score, row in active
                if score
                and score.role_family == "Quant / Financial Technology"
                and not score.rejected
            ],
            "Global Opportunities": [
                row
                for job, score, row in active
                if score
                and (
                    job.work_authorization == "sponsorship_available"
                    or score.details.get("location_eligibility") == "confirmed_eligible"
                )
                and "india" not in (job.location or "").lower()
            ],
            "Needs Review": [
                row
                for _, score, row in active
                if score
                and not score.rejected
                and (
                    score.eligibility_status == "needs_review"
                    or score.details.get("compensation_evidence") == "unknown"
                    or score.details.get("visa_eligibility_evidence") == "unknown"
                )
            ],
            "Applications": list(rows.values()),
        }
        for name, view_rows in views.items():
            self._upsert(name, view_rows, preserve_columns={STATUS_COLUMN, NOTES_COLUMN})
        health = [["Company", "Source", "Last Success", "Last Error"]]
        for source in session.scalars(select(AtsSource)):
            health.append(
                [
                    source.company_id,
                    source.source_type,
                    str(source.last_success_at or ""),
                    source.last_error or "",
                ]
            )
        self._worksheet("Source Health").update(health, "A1", value_input_option="RAW")

    def _row(self, job, companies, scores, applications) -> list[Any]:
        score = scores.get(job.id)
        details = score.details if score else {}
        application = applications.get(job.id)
        salary = (
            "Unknown"
            if job.salary_min is None and job.salary_max is None
            else (
                f"{job.salary_min or ''}-{job.salary_max or ''} {job.salary_currency or ''}"
            ).strip()
        )
        values = [
            job.stable_id,
            companies[job.company_id],
            job.title,
            score.role_family if score else "Other",
            score.primary_language if score else "unknown",
            details.get("experience_fit", "unknown"),
            job.location or "Unknown",
            details.get("location_eligibility", "unknown"),
            salary,
            details.get("visa_eligibility_evidence", job.work_authorization),
            score.score if score else "",
            score.eligibility_status if score else "needs_review",
            ", ".join(score.matched_skills if score else []),
            ", ".join(score.missing_skills if score else []),
            "; ".join(score.reasons if score else []),
            job.source_posted_at.isoformat() if job.source_posted_at else "Unknown",
            job.first_seen_at.isoformat(),
            job.apply_url,
            "",
            job.source,
            application.status if application else "not_applied",
            application.notes if application else "",
            score.application_priority if score else "",
        ]
        return [safe_cell(value) for value in values]

    def _upsert(self, name: str, rows: list[list[Any]], preserve_columns: set[int]) -> None:
        worksheet = self._worksheet(name)
        existing = self._existing(worksheet)
        if not existing:
            worksheet.update([HEADERS], "A1", value_input_option="RAW")
        updates, appends = [], []
        end_column = "W"
        for row in rows:
            if row[0] in existing:
                row_number, old = existing[row[0]]
                for column in preserve_columns:
                    if len(old) > column and old[column]:
                        row[column] = old[column]
                updates.append(
                    {"range": f"A{row_number}:{end_column}{row_number}", "values": [row]}
                )
            else:
                appends.append(row)
        if updates:
            worksheet.batch_update(updates, value_input_option="RAW")
        if appends:
            worksheet.append_rows(appends, value_input_option="RAW")
