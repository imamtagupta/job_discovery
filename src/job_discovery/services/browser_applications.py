import csv
import json
import re
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .application_faq import record_unseen_question, resolve_question

FINAL_SUBMIT_PATTERN = re.compile(
    r"^(submit( application)?|send application|complete application)$", re.IGNORECASE
)
APPLY_PATTERN = re.compile(r"^(apply|apply now|apply for this job)$", re.IGNORECASE)
CONFIRMATION_PATTERN = re.compile(
    r"(application (has been )?(submitted|received)|thank you for applying)", re.IGNORECASE
)


@dataclass
class StagingResult:
    stable_job_id: str
    company: str
    title: str
    url: str
    status: str = "started"
    filled_fields: list[str] = field(default_factory=list)
    unresolved_questions: list[str] = field(default_factory=list)
    skipped_fields: list[str] = field(default_factory=list)
    screenshot: str | None = None
    final_url: str | None = None
    error: str | None = None


def load_queue_job(queue_path: Path, stable_job_id: str | None = None) -> dict[str, str]:
    with queue_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    candidates = [row for row in rows if row.get("status") == "ready_for_review"]
    if stable_job_id:
        candidates = [row for row in candidates if row.get("stable_job_id") == stable_job_id]
    if not candidates:
        target = f" with id {stable_job_id}" if stable_job_id else ""
        raise ValueError(f"No ready_for_review job found{target}")
    return candidates[0]


def load_discovered_jobs(report_path: Path, jobs_path: Path) -> list[dict[str, str]]:
    """Return eligible, unapplied CSV jobs with report recommendations first."""
    report = report_path.read_text(encoding="utf-8")
    report_urls = re.findall(r"^- Apply:\s+(\S+)\s*$", report, flags=re.MULTILINE)
    priority = {url: index for index, url in enumerate(report_urls)}
    with jobs_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    candidates = [
        row
        for row in rows
        if row.get("eligibility_status") == "eligible"
        and row.get("application_status", "not_applied") in {"", "not_applied", "ready_for_review"}
        and row.get("direct_apply_url")
    ]
    return sorted(
        candidates,
        key=lambda row: (
            priority.get(row["direct_apply_url"], len(priority)),
            -float(row.get("application_priority") or 0),
            -float(row.get("match_score") or 0),
        ),
    )


def _location_scope(location: str) -> str:
    return "india" if "india" in location.casefold() else "outside_india"


def _conditional_answer(answer: Any, location: str) -> Any:
    if isinstance(answer, dict):
        return answer.get(_location_scope(location))
    return answer


def answer_for_question(
    question: str,
    *,
    profile: dict[str, Any],
    faq_path: Path,
    location: str,
) -> tuple[str, Any]:
    normalized = " ".join(re.findall(r"[a-z0-9]+", question.casefold()))
    candidate = profile.get("candidate", {})
    application = profile.get("application", {})
    direct_answers = [
        (("first name", "given name"), application.get("first_name")),
        (("last name", "family name", "surname"), application.get("last_name")),
        (("full name", "name"), candidate.get("name")),
        (("email", "email address"), application.get("email")),
        (("phone", "phone number", "mobile", "mobile number"), application.get("phone")),
        (("linkedin", "linkedin profile"), application.get("linkedin")),
        (("current location", "location", "city"), candidate.get("location")),
    ]
    for aliases, answer in direct_answers:
        if answer and normalized in aliases:
            return "approved", answer

    resolved = resolve_question(faq_path, question)
    answer = _conditional_answer(resolved.get("answer"), location)
    if resolved["status"] == "approved" and answer is not None:
        return "approved", answer
    return resolved["status"], answer


def _field_question(locator: Any) -> str:
    label_text = locator.evaluate(
        r"""el => {
            if (el.labels && el.labels.length) {
                return Array.from(el.labels).map(label => label.innerText).join(' ');
            }
            const labelledBy = el.getAttribute('aria-labelledby');
            if (labelledBy) {
                return labelledBy.split(/\s+/).map(id => {
                    const node = document.getElementById(id);
                    return node ? node.innerText : '';
                }).join(' ');
            }
            return '';
        }"""
    ).strip()
    if label_text:
        return label_text
    for attribute in ("aria-label", "placeholder", "name"):
        value = locator.get_attribute(attribute)
        if value:
            return value.strip()
    return "Unlabelled application field"


def _choose_option(locator: Any, answer: Any) -> bool:
    text = str(answer)
    try:
        locator.select_option(label=text)
        return True
    except Exception:
        try:
            locator.select_option(value=text)
            return True
        except Exception:
            options = locator.locator("option").all()
            for option in options:
                label = option.inner_text().strip()
                if label.casefold() == text.casefold():
                    locator.select_option(value=option.get_attribute("value"))
                    return True
    return False


def _stage_page(
    page: Any,
    *,
    row: dict[str, str],
    profile: dict[str, Any],
    faq_path: Path,
    backlog_path: Path,
    resume_path: Path,
    result: StagingResult,
) -> None:
    file_inputs = page.locator('input[type="file"]')
    for index in range(file_inputs.count()):
        field = file_inputs.nth(index)
        question = _field_question(field)
        normalized = question.casefold()
        is_resume = any(word in normalized for word in ("resume", "résumé", "cv"))
        if field.is_enabled() and (is_resume or file_inputs.count() == 1):
            field.set_input_files(str(resume_path.resolve()))
            result.filled_fields.append(question)
        elif field.is_enabled():
            result.unresolved_questions.append(question)
            record_unseen_question(backlog_path, question, row.get("direct_apply_url"))

    fields = page.locator("input:not([type=hidden]):not([type=file]), textarea, select")
    seen_questions: set[str] = set()
    for index in range(fields.count()):
        locator = fields.nth(index)
        try:
            if not locator.is_visible() or not locator.is_enabled():
                continue
            field_type = (
                locator.get_attribute("type") or locator.evaluate("el => el.tagName")
            ).lower()
            if field_type in {"submit", "button", "reset", "checkbox", "radio"}:
                continue
            if locator.input_value().strip():
                continue
            question = _field_question(locator)
            status, answer = answer_for_question(
                question,
                profile=profile,
                faq_path=faq_path,
                location=row.get("location", ""),
            )
            if status == "approved" and answer is not None:
                if field_type == "select":
                    filled = _choose_option(locator, answer)
                else:
                    locator.fill(str(answer))
                    filled = True
                if filled:
                    result.filled_fields.append(question)
                    continue
            if question not in seen_questions:
                seen_questions.add(question)
                result.unresolved_questions.append(question)
                record_unseen_question(backlog_path, question, row.get("direct_apply_url"))
        except Exception as exc:
            result.skipped_fields.append(f"{_field_question(locator)}: {type(exc).__name__}")

    required_choices = page.locator(
        'input[type="checkbox"][required], input[type="radio"][required]'
    )
    for index in range(required_choices.count()):
        choice = required_choices.nth(index)
        if choice.is_visible() and choice.is_enabled() and not choice.is_checked():
            question = _field_question(choice)
            if question not in seen_questions:
                seen_questions.add(question)
                result.unresolved_questions.append(question)
                record_unseen_question(backlog_path, question, row.get("direct_apply_url"))


def stage_application(
    row: dict[str, str],
    *,
    profile: dict[str, Any],
    faq_path: Path,
    backlog_path: Path,
    resume_path: Path,
    artifact_dir: Path,
    headless: bool = False,
    submit: bool = False,
    timeout_ms: int = 30_000,
) -> StagingResult:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e '.[applications]' && "
            "playwright install chromium"
        ) from exc

    artifact_dir.mkdir(parents=True, exist_ok=True)
    result = StagingResult(
        stable_job_id=row["stable_job_id"],
        company=row["company"],
        title=row["title"],
        url=row["direct_apply_url"],
    )
    safe_id = re.sub(r"[^a-zA-Z0-9_-]", "_", row["stable_job_id"][:16])
    screenshot_path = artifact_dir / f"{safe_id}.png"
    state_path = artifact_dir / "browser-state.json"
    try:
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(headless=headless)
            context_args: dict[str, Any] = {}
            if state_path.exists():
                context_args["storage_state"] = str(state_path)
            context = browser.new_context(**context_args)
            page = context.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(row["direct_apply_url"], wait_until="domcontentloaded")
            page.wait_for_timeout(1500)

            if page.locator('input[type="file"]').count() == 0:
                apply_controls = page.get_by_role("button", name=APPLY_PATTERN).or_(
                    page.get_by_role("link", name=APPLY_PATTERN)
                )
                if apply_controls.count():
                    apply_controls.first.click()
                    page.wait_for_load_state("domcontentloaded")
                    page.wait_for_timeout(1000)

            form_scope = page
            for frame in page.frames:
                if frame.locator('input[type="file"]').count():
                    form_scope = frame
                    break

            _stage_page(
                form_scope,
                row=row,
                profile=profile,
                faq_path=faq_path,
                backlog_path=backlog_path,
                resume_path=resume_path,
                result=result,
            )

            submit_controls = form_scope.get_by_role("button", name=FINAL_SUBMIT_PATTERN)
            if result.unresolved_questions:
                result.status = "needs_input"
            elif submit_controls.count() == 0:
                result.status = "staged_for_review"
                result.skipped_fields.append("No final submit control detected on current page")
            elif not submit:
                result.status = "staged_for_review"
            else:
                submit_controls.first.click()
                page.wait_for_timeout(2000)
                confirmation = form_scope.get_by_text(CONFIRMATION_PATTERN)
                if confirmation.count() or CONFIRMATION_PATTERN.search(
                    form_scope.locator("body").inner_text()
                ):
                    result.status = "submitted"
                else:
                    result.status = "submission_attempted"
                    result.skipped_fields.append(
                        "Submit was clicked but no application confirmation was detected"
                    )
            result.final_url = page.url
            page.screenshot(path=str(screenshot_path), full_page=True)
            result.screenshot = str(screenshot_path)
            context.storage_state(path=str(state_path))
            context.close()
            browser.close()
    except Exception as exc:
        result.status = "failed"
        result.error = f"{type(exc).__name__}: {exc}"
    return result


def write_staging_result(result: StagingResult, artifact_dir: Path) -> Path:
    artifact_dir.mkdir(parents=True, exist_ok=True)
    timestamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = artifact_dir / f"application-{result.stable_job_id[:12]}-{timestamp}.json"
    path.write_text(json.dumps(asdict(result), indent=2, ensure_ascii=False) + "\n")
    return path
