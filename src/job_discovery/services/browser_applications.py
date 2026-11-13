import csv
import json
import re
import subprocess
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .application_faq import record_unseen_question, resolve_question

FINAL_SUBMIT_PATTERN = re.compile(
    r"^(submit( application)?|send application|complete application)$", re.IGNORECASE
)
APPLY_PATTERN = re.compile(r"^(apply|apply now|apply for this job)$", re.IGNORECASE)
START_APPLICATION_PATTERN = re.compile(
    r"^(autofill with resume|apply manually|start application)$", re.IGNORECASE
)
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


def load_discovered_jobs(
    report_path: Path,
    jobs_path: Path,
    *,
    include_needs_review: bool = False,
    retry_incomplete: bool = False,
) -> list[dict[str, str]]:
    """Return eligible, unapplied CSV jobs with report recommendations first."""
    report = report_path.read_text(encoding="utf-8")
    report_urls = re.findall(r"^- Apply:\s+(\S+)\s*$", report, flags=re.MULTILINE)
    priority = {url: index for index, url in enumerate(report_urls)}
    with jobs_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))

    allowed_eligibility = {"eligible", "needs_review"} if include_needs_review else {"eligible"}
    allowed_statuses = {"", "not_applied", "ready_for_review"}
    if retry_incomplete:
        allowed_statuses.update(
            {
                "needs_input",
                "staged_for_review",
                "submission_attempted",
                "failed_validation",
                "failed",
            }
        )
    candidates = [
        row
        for row in rows
        if row.get("eligibility_status") in allowed_eligibility
        and row.get("application_status", "not_applied") in allowed_statuses
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
    city = str(candidate.get("location") or "").split(",", 1)[0].strip()
    direct_answers = [
        (("first name", "given name", "given name s"), application.get("first_name")),
        (
            ("last name", "family name", "surname", "local family name"),
            application.get("last_name"),
        ),
        (("full name", "name"), candidate.get("name")),
        (("first and last name", "legal name"), candidate.get("name")),
        (("preferred name", "local given name s"), application.get("preferred_name")),
        (("email", "email address"), application.get("email")),
        (("phone", "phone number", "mobile", "mobile number"), application.get("phone")),
        (("linkedin", "linkedin profile"), application.get("linkedin")),
        (("github", "github url", "github profile"), application.get("github")),
        (("pronouns",), application.get("pronouns")),
        (("passport country",), application.get("passport_country")),
        (("country of residence",), application.get("residence_country")),
        (("country",), application.get("country")),
        (("current location", "location"), candidate.get("location")),
        (("location city", "city"), city),
    ]
    for aliases, answer in direct_answers:
        if answer and normalized in aliases:
            return "approved", answer

    for entry in application.get("answers", {}).values():
        questions = [entry.get("question", ""), *entry.get("aliases", [])]
        if normalized in {
            " ".join(re.findall(r"[a-z0-9]+", item.casefold())) for item in questions
        }:
            answer = _conditional_answer(entry.get("answer"), location)
            return entry.get("status", "needs_review"), answer

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


def _normalized_choice(value: Any) -> str:
    return " ".join(re.findall(r"[a-z0-9]+", str(value).casefold()))


def _choice_matches(wanted: str, option: str) -> bool:
    if option == wanted or option.startswith(wanted) or wanted.startswith(option):
        return True
    if wanted in {"company careers page", "company website"} and option == "career site":
        return True
    if wanted == "no" and (
        "not a protected veteran" in option or "not a veteran" in option
    ):
        return True
    if wanted in {"decline to self identify", "prefer not to say"}:
        return any(
            phrase in option
            for phrase in ("decline", "don t wish", "prefer not", "do not wish")
        )
    return False


def _requires_nested_choice(wanted: str) -> bool:
    return wanted in {"career site", "company careers page", "company website"}


def _selection_instruction_committed(instruction: str, label: str) -> bool:
    normalized = instruction.strip().casefold()
    return (
        not normalized.startswith("0 items selected")
        and "item selected" in normalized
        and label.casefold() in normalized
    )


def _is_selectable_prompt_option(option: Any) -> bool:
    """Reject Workday prompt headings that reuse an option's visible text."""
    if option.get_attribute("aria-disabled") == "true":
        return False
    role = (option.get_attribute("role") or "").casefold()
    if role in {"option", "radio", "checkbox"}:
        return True
    return bool(
        option.locator(
            'input[type="radio"], input[type="checkbox"], [role="radio"], [role="option"]'
        ).count()
    )


def _workday_multiselect_container(scope: Any, locator: Any) -> Any | None:
    multiselect_id = locator.get_attribute("data-uxi-multiselect-id")
    if not multiselect_id:
        return None
    container = scope.locator(
        f'[data-automation-id="multiSelectContainer"][id="{multiselect_id}"]'
    )
    return container if container.count() else None


def _nested_choice_committed(scope: Any, locator: Any, label: str) -> bool:
    container = _workday_multiselect_container(scope, locator)
    if container is None:
        return False
    instruction = container.locator('[data-automation-id="promptAriaInstruction"]')
    if instruction.count() and _selection_instruction_committed(
        instruction.inner_text(), label
    ):
        return True
    selected_items = container.locator('[data-automation-id="selectedItem"]')
    for index in range(selected_items.count()):
        selected = selected_items.nth(index)
        if label.casefold() in (selected.get_attribute("aria-label") or "").casefold():
            return True
    return False


def _scoped_options(scope: Any, locator: Any) -> Any:
    controls = locator.get_attribute("aria-controls")
    if controls:
        listbox = scope.locator(f"#{controls}")
        if listbox.count():
            return listbox.get_by_role("option")
    return scope.get_by_role("option")


def _choose_combobox_option(scope: Any, locator: Any, answer: Any) -> bool:
    workday_container = _workday_multiselect_container(scope, locator)
    if workday_container is not None:
        prompt_icon = workday_container.locator('[data-automation-id="promptIcon"]')
        if not prompt_icon.count():
            return False
        prompt_icon.click()
    else:
        locator.click()
    scope.wait_for_timeout(300)
    # Greenhouse location fields populate their listbox only after typing.
    # Ordinary select-like controls reject fill(), so retaining the opened list
    # is a safe fallback for those controls.
    editable_location = "location" in _normalized_choice(_field_question(locator))
    filled_editable = False
    try:
        if editable_location and locator.get_attribute("readonly") is None:
            locator.fill("")
            locator.press_sequentially(str(answer), delay=75)
            scope.wait_for_timeout(1_000)
            filled_editable = locator.input_value().strip() == str(answer).strip()
    except Exception:
        pass
    if workday_container is not None:
        options = scope.locator('[data-automation-id="promptOption"]')
    else:
        options = _scoped_options(scope, locator)
    wanted = _normalized_choice(answer)
    fallback = None
    for index in range(options.count()):
        option = options.nth(index)
        label = option.inner_text().strip()
        normalized = _normalized_choice(label)
        if _choice_matches(wanted, normalized):
            option.click()
            if _requires_nested_choice(wanted):
                scope.wait_for_timeout(500)
                nested_rows = scope.locator(
                    '[data-automation-id="promptOption"]'
                    '[data-automation-label="Career Site"]'
                )
                nested_selected = False
                for nested_index in range(nested_rows.count() - 1, -1, -1):
                    nested_row = nested_rows.nth(nested_index)
                    # Workday's nested row has data-automation-label but often no
                    # ARIA role. The same-text heading does not have this label.
                    if nested_row.is_visible():
                        nested_row.click()
                        nested_selected = True
                        break
                if not nested_selected:
                    nested_radio = scope.get_by_role(
                        "radio", name="Career Site", exact=True
                    )
                    if nested_radio.count():
                        nested_radio.last.check(force=True)
                        nested_selected = True
                if not nested_selected:
                    nested_label = scope.get_by_label("Career Site", exact=True)
                    radio_labels = nested_label.locator('input[type="radio"]')
                    if radio_labels.count():
                        radio_labels.last.check(force=True)
                        nested_selected = True
                    elif nested_label.count():
                        nested_label.last.click()
                        nested_selected = True
                if not nested_selected:
                    return False
                scope.wait_for_timeout(500)
                if not _nested_choice_committed(scope, locator, "Career Site"):
                    return False
            return True
        if fallback is None and (
            normalized.startswith(wanted) or wanted.startswith(normalized)
        ):
            fallback = option
    if fallback is not None:
        fallback.click()
        return True
    if filled_editable and options.count() == 0:
        return True
    locator.press("Escape")
    return False


def _browser_profile_key(row: dict[str, str]) -> str:
    company = re.sub(r"[^a-z0-9]+", "-", row.get("company", "employer").casefold()).strip("-")
    provider = row.get("ats_provider", "browser") or "browser"
    return f"{provider}-{company}"


def _workday_keychain_service(company: str) -> str:
    aliases = {
        "palo alto networks": "paloaltonetworks",
        "nvidia": "nvidia",
        "citi": "citi",
        "crowdstrike": "crowdstrike",
        "barclays": "barclays",
        "target": "target",
        "red hat": "redhat",
    }
    return f"job-discovery-workday-{aliases.get(company.casefold(), company.casefold())}"


def _sign_in_workday(page: Any, profile: dict[str, Any], result: StagingResult) -> bool:
    email = profile.get("application", {}).get("email")
    if not email:
        return False
    email_sign_in = page.get_by_role("button", name="Sign in with email", exact=True)
    if email_sign_in.count():
        email_sign_in.first.click()
        page.wait_for_timeout(500)
    elif page.locator('[data-automation-id="signInLink"]').count():
        page.locator('[data-automation-id="signInLink"]').click()
        page.wait_for_timeout(500)
    password_input = page.locator('[data-automation-id="password"]')
    if not password_input.count():
        return True
    try:
        password = subprocess.check_output(
            [
                "security",
                "find-generic-password",
                "-a",
                email,
                "-s",
                _workday_keychain_service(result.company),
                "-w",
            ],
            text=True,
            stderr=subprocess.DEVNULL,
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        result.status = "needs_input"
        result.unresolved_questions.append(
            f"Workday account credentials unavailable for {result.company}"
        )
        return False
    for locator, value in (
        (page.locator('[data-automation-id="email"]'), email),
        (password_input, password),
    ):
        locator.click()
        locator.fill("")
        locator.press_sequentially(value, delay=15)
        locator.press("Tab")
    submit = page.get_by_role("button", name="Submit", exact=True)
    if not submit.count():
        return False
    box = submit.bounding_box()
    if box:
        page.mouse.click(box["x"] + box["width"] / 2, box["y"] + box["height"] / 2)
    else:
        submit.click()
    page.wait_for_timeout(7_000)
    if page.get_by_text(re.compile(r"verify your account", re.I)).count():
        result.status = "needs_input"
        result.unresolved_questions.append(
            f"Workday email verification required for {result.company}"
        )
        return False
    return page.locator('[data-automation-id="password"]').count() == 0


def _enter_application(
    page: Any, result: StagingResult, profile: dict[str, Any]
) -> bool:
    """Navigate a job landing page into the actual application form."""
    try:
        page.wait_for_load_state("networkidle", timeout=60_000)
    except Exception:
        page.wait_for_timeout(5_000)

    if page.locator('input[type="file"]').count():
        return True

    # MongoDB embeds its Greenhouse form after the marketing page loads. The
    # embed occasionally misses its first initialization, so retry once before
    # treating the page as form-less.
    if "mongodb.com/careers/jobs/" in page.url:
        try:
            page.locator('input[type="file"]').wait_for(state="attached", timeout=10_000)
        except Exception:
            page.reload(wait_until="domcontentloaded")
            page.wait_for_timeout(8_000)
        if page.locator('input[type="file"]').count():
            return True

    apply_controls = page.get_by_role("button", name=APPLY_PATTERN).or_(
        page.get_by_role("link", name=APPLY_PATTERN)
    )
    if apply_controls.count():
        apply_controls.first.click()
        page.wait_for_timeout(750)

    # Workday opens a second "Start Your Application" dialog.
    start_controls = page.get_by_role("button", name=START_APPLICATION_PATTERN)
    if start_controls.count():
        preferred = page.get_by_role("button", name="Autofill with Resume", exact=True)
        (preferred.first if preferred.count() else start_controls.first).click()
        try:
            page.wait_for_url(re.compile(r"/apply/"), timeout=30_000)
        except Exception:
            pass
        page.wait_for_timeout(8_000)

    # Workday accounts are tenant-specific. Identify this gate explicitly so a
    # landing page can never be mistaken for a staged application.
    if "myworkdayjobs.com" in page.url and (
        page.locator('[data-automation-id="password"]').count()
        or page.get_by_role("button", name="Sign in with email", exact=True).count()
    ):
        return _sign_in_workday(page, profile, result)

    return True


def _workday_step(page: Any) -> int | None:
    match = re.search(r"current step\s+(\d+)\s+of\s+\d+", page.locator("body").inner_text(), re.I)
    return int(match.group(1)) if match else None


def _workday_validation_messages(page: Any) -> list[str]:
    body = page.locator("body").inner_text()
    if "Errors Found" not in body:
        return []
    messages: list[str] = []
    candidates = page.locator(
        '[data-automation-id="errorMessage"], [id$="-error"], '
        'a:has-text("Error-"), a:has-text("Error -")'
    )
    for index in range(candidates.count()):
        candidate = candidates.nth(index)
        if candidate.is_visible():
            message = candidate.inner_text().strip()
            if message and message not in messages:
                messages.append(message)
    if not messages:
        for label in re.findall(r"Error\s*-\s*([^\n]+)", body, re.I):
            message = label.strip()
            if message and message not in messages:
                messages.append(message)
    return messages or ["Workday rejected the current step; see the saved screenshot"]


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
    resume_input = page.locator('input[type="file"]#resume')
    if resume_input.count() == 0:
        resume_input = page.locator('input[type="file"]').first
    if resume_input.count() and resume_input.is_enabled():
        resume_input.set_input_files(str(resume_path.resolve()))
        result.filled_fields.append("Resume/CV")

    seen_questions: set[str] = set()
    checkboxes = page.locator('input[type="checkbox"]')
    for index in range(checkboxes.count()):
        locator = checkboxes.nth(index)
        if not locator.is_visible() or not locator.is_enabled() or locator.is_checked():
            continue
        question = _field_question(locator)
        status, answer = answer_for_question(
            question,
            profile=profile,
            faq_path=faq_path,
            location=row.get("location", ""),
        )
        if status == "approved" and str(answer).casefold() in {"yes", "true", "1"}:
            locator.check()
            result.filled_fields.append(question)

    previous_worker = page.locator(
        'input[type="radio"][name*="previousWorker" i][value="false"], '
        'input[type="radio"][name*="previousWorker" i][value="No" i]'
    )
    if previous_worker.count() and not previous_worker.is_checked():
        previous_worker.check(force=True)
    if previous_worker.count() and previous_worker.is_checked():
        result.filled_fields.append("Previously worked for this organization: No")

    comboboxes = page.get_by_role("combobox")
    for index in range(comboboxes.count()):
        locator = comboboxes.nth(index)
        if not locator.is_visible() or not locator.is_enabled():
            continue
        question = _field_question(locator)
        status, answer = answer_for_question(
            question,
            profile=profile,
            faq_path=faq_path,
            location=row.get("location", ""),
        )
        if status == "approved" and answer is not None:
            if _choose_combobox_option(page, locator, answer):
                result.filled_fields.append(question)
                continue
        is_required = locator.get_attribute("aria-required") == "true" or "*" in question
        if is_required and question not in seen_questions:
            seen_questions.add(question)
            result.unresolved_questions.append(question)
            record_unseen_question(backlog_path, question, row.get("direct_apply_url"))

    fields = page.locator("input:not([type=hidden]):not([type=file]), textarea, select")
    for index in range(fields.count()):
        locator = fields.nth(index)
        try:
            if not locator.is_visible() or not locator.is_enabled():
                continue
            if locator.get_attribute("role") == "combobox":
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
                elif locator.get_attribute("aria-autocomplete") == "list":
                    filled = _choose_combobox_option(page, locator, answer)
                else:
                    locator.fill(str(answer))
                    filled = True
                if filled:
                    result.filled_fields.append(question)
                    continue
            if question not in seen_questions:
                is_required = (
                    locator.get_attribute("required") is not None
                    or locator.get_attribute("aria-required") == "true"
                    or "*" in question
                )
                if question.casefold().startswith("local ") and "*" not in question:
                    is_required = False
                if is_required:
                    seen_questions.add(question)
                    result.unresolved_questions.append(question)
                    record_unseen_question(backlog_path, question, row.get("direct_apply_url"))
                else:
                    result.skipped_fields.append(f"Optional field left blank: {question}")
        except Exception as exc:
            result.skipped_fields.append(f"{_field_question(locator)}: {type(exc).__name__}")

    required_choices = page.locator(
        'input[type="checkbox"][required], input[type="radio"][required]'
    )
    for index in range(required_choices.count()):
        choice = required_choices.nth(index)
        if choice.is_visible() and choice.is_enabled() and not choice.is_checked():
            question = _field_question(choice)
            status, answer = answer_for_question(
                question,
                profile=profile,
                faq_path=faq_path,
                location=row.get("location", ""),
            )
            if status == "approved" and str(answer).casefold() in {"yes", "true", "1"}:
                choice.check()
                result.filled_fields.append(question)
                continue
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
    verification_code: str | None = None,
    interactive_verification: bool = False,
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
            profile_dir = artifact_dir / "browser-profiles" / _browser_profile_key(row)
            profile_dir.mkdir(parents=True, exist_ok=True)
            context = playwright.chromium.launch_persistent_context(
                str(profile_dir), headless=headless
            )
            page = context.pages[0] if context.pages else context.new_page()
            page.set_default_timeout(timeout_ms)
            page.goto(row["direct_apply_url"], wait_until="domcontentloaded")
            entered_application = _enter_application(page, result, profile)

            form_scope = page
            for frame in page.frames:
                if frame.locator('input[type="file"]').count():
                    form_scope = frame
                    break

            if entered_application:
                for _ in range(8):
                    _stage_page(
                        form_scope,
                        row=row,
                        profile=profile,
                        faq_path=faq_path,
                        backlog_path=backlog_path,
                        resume_path=resume_path,
                        result=result,
                    )
                    if result.unresolved_questions or "myworkdayjobs.com" not in page.url:
                        break
                    next_button = form_scope.locator(
                        '[data-automation-id="pageFooterNextButton"]'
                    )
                    if not next_button.count():
                        break
                    next_text = next_button.inner_text().strip().casefold()
                    if "submit" in next_text:
                        break
                    previous_step = _workday_step(page)
                    next_button.click()
                    page.wait_for_timeout(4_000)
                    current_step = _workday_step(page)
                    if (
                        previous_step is not None
                        and current_step is not None
                        and current_step == previous_step
                    ):
                        messages = _workday_validation_messages(page)
                        if messages:
                            result.status = "failed_validation"
                            result.unresolved_questions.extend(
                                message
                                for message in messages
                                if message not in result.unresolved_questions
                            )
                            break
                    validation_errors = form_scope.locator(
                        '[role="alert"], [data-automation-id="errorMessage"]'
                    )
                    messages: list[str] = []
                    for error_index in range(validation_errors.count()):
                        error = validation_errors.nth(error_index)
                        if error.is_visible():
                            message = error.inner_text().strip()
                            if message and message not in messages:
                                messages.append(message)
                    if messages:
                        result.unresolved_questions.extend(
                            message
                            for message in messages
                            if message not in result.unresolved_questions
                        )
                        break

            submit_controls = form_scope.get_by_role("button", name=FINAL_SUBMIT_PATTERN)
            if not entered_application:
                pass
            elif result.status == "failed_validation":
                pass
            elif result.unresolved_questions:
                result.status = "needs_input"
            elif submit_controls.count() == 0:
                result.status = "staged_for_review"
                result.skipped_fields.append("No final submit control detected on current page")
            elif not submit:
                result.status = "staged_for_review"
            else:
                submit_controls.first.click()
                page.wait_for_timeout(5000)
                code_inputs = form_scope.locator('input[maxlength="1"]')
                current_code = verification_code
                if interactive_verification and code_inputs.count() and not current_code:
                    current_code = input("Verification code: ").strip()
                if current_code and code_inputs.count() == len(current_code):
                    for index, character in enumerate(current_code):
                        code_inputs.nth(index).fill(character)
                    result.filled_fields.append("Email verification code")
                    submit_controls.first.click()
                    page.wait_for_timeout(5000)
                confirmation = form_scope.get_by_text(CONFIRMATION_PATTERN)
                if (
                    "/confirmation" in page.url
                    or confirmation.count()
                    or CONFIRMATION_PATTERN.search(form_scope.locator("body").inner_text())
                ):
                    result.status = "submitted"
                else:
                    validation_errors = form_scope.locator(
                        '[role="alert"], [id$="-error"], .field-error'
                    )
                    messages = []
                    for index in range(validation_errors.count()):
                        error = validation_errors.nth(index)
                        if error.is_visible():
                            message = error.inner_text().strip()
                            if message and message not in messages:
                                messages.append(message)
                    if messages:
                        result.status = "failed_validation"
                        result.unresolved_questions.extend(messages)
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
