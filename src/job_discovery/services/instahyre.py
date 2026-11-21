import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

INSTAHYRE_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/158.0.0.0 Safari/537.36"
)


@dataclass
class InstahyreResult:
    job_id: int | None
    company: str
    title: str
    location: str
    url: str
    experience: str | None
    status: str
    error: str | None = None


def experience_includes(experience: str | None, years: int) -> bool:
    if not experience:
        return False
    value = experience.strip().replace(" ", "")
    if value.endswith("+"):
        try:
            return years >= int(value[:-1])
        except ValueError:
            return False
    if "-" in value:
        lower, upper = value.split("-", 1)
        try:
            return int(lower) <= years <= int(upper)
        except ValueError:
            return False
    try:
        return int(value) == years
    except ValueError:
        return False


def _experience_from_html(html: str) -> str | None:
    import re

    patterns = (
        r"with ([0-9+ -]+) years of experience",
        r'alt="">\s*([0-9+ -]+) Years',
    )
    for pattern in patterns:
        match = re.search(pattern, html, flags=re.IGNORECASE)
        if match:
            return match.group(1).strip()
    return None


def _job_cards(page: Any) -> list[dict[str, Any]]:
    return page.evaluate(
        """() => Array.from(document.querySelectorAll('a.row.text-link'))
          .map(el => window.angular && window.angular.element(el).scope().opp)
          .filter(Boolean)
          .map(opp => ({
            id: opp.job.id,
            title: opp.job.title,
            company: opp.employer.company_name,
            location: opp.job.locations,
            url: opp.job.public_url,
            keywords: opp.job.keywords || [],
            locationMatch: Boolean(opp.is_location_match)
          }))"""
    )


def _select_page(page: Any, number: int) -> None:
    selected = page.evaluate(
        """number => {
          const card = document.querySelector('a.row.text-link');
          if (!card || !window.angular) return false;
          let scope = window.angular.element(card).scope();
          while (scope && typeof scope.nthPage !== 'function') scope = scope.$parent;
          if (!scope) return false;
          scope.nthPage(number);
          return true;
        }""",
        number,
    )
    if not selected:
        raise RuntimeError("Instahyre pagination controls were not available")
    page.wait_for_timeout(1_500)


def _search_python_jobs(page: Any) -> None:
    search = page.get_by_role("textbox", name="Enter skills or title, e.g Java, C++")
    search.wait_for(state="visible", timeout=20_000)
    search.fill("Python")
    page.get_by_role("button", name="Search").evaluate(
        """element => {
          let scope = window.angular && window.angular.element(element).scope();
          while (scope && typeof scope.searchCustomJobs !== 'function') scope = scope.$parent;
          if (scope) {
            scope.searchCustomJobs(undefined, null, null, true);
            return true;
          }
          element.click();
          return false;
        }"""
    )
    page.wait_for_function(
        """() => location.search.includes('search=true') &&
          document.body.innerText.includes('Search Results')""",
        timeout=20_000,
    )
    page.wait_for_timeout(1_500)


def _apply(page: Any, job: dict[str, Any], experience: str | None) -> InstahyreResult:
    result = InstahyreResult(
        job_id=job.get("id"),
        company=job.get("company", ""),
        title=job.get("title", ""),
        location=job.get("location", ""),
        url=job.get("url", ""),
        experience=experience,
        status="failed",
    )
    try:
        page.goto(result.url, wait_until="domcontentloaded", timeout=30_000)
        page.wait_for_timeout(1_200)
        if page.get_by_role("button", name="Application sent!").count():
            result.status = "already_applied"
            return result

        outcome = page.evaluate(
            """() => {
              const buttons = Array.from(document.querySelectorAll('button'));
              const button = buttons.find(el => el.innerText.trim() === 'Apply now');
              if (!button) return 'no_apply_button';
              let scope = window.angular && window.angular.element(button).scope();
              while (scope && typeof scope.submitChoiceNonMatching !== 'function') {
                scope = scope.$parent;
              }
              if (!scope) return 'no_apply_function';
              scope.submitChoiceNonMatching();
              return 'called';
            }"""
        )
        page.wait_for_timeout(1_500)
        if page.get_by_role("button", name="Application sent!").count():
            result.status = "submitted"
        else:
            result.status = "skipped"
            result.error = f"Submission was not visibly confirmed ({outcome})"
    except Exception as exc:  # Playwright errors must not stop the remaining batch.
        result.status = "failed"
        result.error = str(exc)
    return result


def capture_instahyre_storage_state(output: Path) -> None:
    """Open a local browser for login and save reusable Playwright authentication state."""
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e '.[applications]' && "
            "playwright install chromium"
        ) from exc

    output.parent.mkdir(parents=True, exist_ok=True)
    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=False)
        context = browser.new_context()
        page = context.new_page()
        page.goto("https://www.instahyre.com/login/", wait_until="domcontentloaded")
        print("Log in to Instahyre in the browser; the session will save automatically.")
        for _ in range(600):
            if page.locator('a[href="/logout/"]').count():
                break
            page.wait_for_timeout(1_000)
        else:
            browser.close()
            raise RuntimeError(
                "Instahyre login was not detected within 10 minutes; storage state was not saved"
            )
        state = context.storage_state()
        state["cookies"] = [
            cookie
            for cookie in state.get("cookies", [])
            if cookie.get("domain", "").lstrip(".").endswith("instahyre.com")
        ]
        state["origins"] = [
            origin
            for origin in state.get("origins", [])
            if (urlparse(origin.get("origin", "")).hostname or "").endswith("instahyre.com")
        ]
        output.write_text(json.dumps(state, indent=2), encoding="utf-8")
        output.chmod(0o600)
        browser.close()


def run_instahyre_applications(
    *,
    storage_state: Path,
    output: Path,
    limit: int = 100,
    candidate_years: int = 6,
    max_search_pages: int = 20,
    dry_run: bool = False,
    user_agent: str = INSTAHYRE_USER_AGENT,
) -> dict[str, Any]:
    try:
        from playwright.sync_api import sync_playwright
    except ImportError as exc:
        raise RuntimeError(
            "Playwright is not installed. Run: pip install -e '.[applications]' && "
            "playwright install chromium"
        ) from exc

    if not storage_state.is_file():
        raise FileNotFoundError(f"Instahyre storage state not found: {storage_state}")

    output.mkdir(parents=True, exist_ok=True)
    results: list[InstahyreResult] = []
    seen: set[int] = set()
    submitted = 0

    with sync_playwright() as playwright:
        browser = playwright.chromium.launch(headless=True)
        context = browser.new_context(storage_state=str(storage_state), user_agent=user_agent)
        page = context.new_page()
        page.goto(
            "https://www.instahyre.com/candidate/opportunities/"
            "?company_size=0&job_type=0&search=true&years=6",
            wait_until="domcontentloaded",
            timeout=30_000,
        )
        if page.locator('a[href="/logout/"]').count() == 0:
            browser.close()
            raise RuntimeError(
                "Instahyre session is not authenticated; refresh INSTAHYRE_STORAGE_STATE_JSON"
            )

        _search_python_jobs(page)

        for page_number in range(1, max_search_pages + 1):
            if page_number > 1:
                _select_page(page, page_number)
            cards = _job_cards(page)
            if not cards:
                break

            candidates: list[tuple[dict[str, Any], str]] = []
            for job in cards:
                job_id = int(job["id"])
                if job_id in seen or not job.get("locationMatch"):
                    continue
                seen.add(job_id)
                try:
                    details = page.request.get(
                        f"https://www.instahyre.com/api/v1/employer_public_jobs/{job_id}"
                    ).json()
                    opportunity_url = details.get("opportunity_url")
                    if opportunity_url:
                        job["url"] = f"https://www.instahyre.com{opportunity_url}"
                    if not job.get("url"):
                        raise RuntimeError("Job details did not provide a public URL")
                except Exception as exc:
                    results.append(
                        InstahyreResult(
                            job_id=job_id,
                            company=job.get("company", ""),
                            title=job.get("title", ""),
                            location=job.get("location", ""),
                            url=job.get("url", ""),
                            experience=None,
                            status="failed",
                            error=str(exc),
                        )
                    )
                    continue
                if details.get("applied_on"):
                    continue
                minimum = details.get("workex_min")
                maximum = details.get("workex_max")
                experience = (
                    f"{minimum}-{maximum}" if minimum is not None and maximum is not None else None
                )
                searchable = " ".join(
                    [
                        str(details.get("description") or ""),
                        " ".join(details.get("keywords") or []),
                    ]
                )
                if "python" not in searchable.casefold():
                    continue
                if not experience_includes(experience, candidate_years):
                    continue
                candidates.append((job, experience or ""))

            for job, experience in candidates:
                if submitted >= limit or (dry_run and len(results) >= limit):
                    break
                if dry_run:
                    result = InstahyreResult(
                        job_id=job.get("id"),
                        company=job.get("company", ""),
                        title=job.get("title", ""),
                        location=job.get("location", ""),
                        url=job.get("url", ""),
                        experience=experience,
                        status="eligible",
                    )
                else:
                    result = _apply(page, job, experience)
                results.append(result)
                if result.status == "submitted":
                    submitted += 1
            if submitted >= limit or (dry_run and len(results) >= limit):
                break

            # Application navigation leaves the search page; return before selecting another page.
            page.goto(
                "https://www.instahyre.com/candidate/opportunities/"
                "?company_size=0&job_type=0&search=true&years=6",
                wait_until="domcontentloaded",
                timeout=30_000,
            )
            _search_python_jobs(page)

        browser.close()

    totals: dict[str, int] = {}
    for result in results:
        totals[result.status] = totals.get(result.status, 0) + 1
    report = {
        "candidate_years": candidate_years,
        "dry_run": dry_run,
        "limit": limit,
        "reviewed": len(seen),
        "results": totals,
        "applications": [asdict(result) for result in results],
    }
    (output / "summary.json").write_text(json.dumps(report, indent=2), encoding="utf-8")
    return report


def main() -> None:
    parser = argparse.ArgumentParser(description="Run the Instahyre application workflow")
    parser.add_argument("--storage-state", default="instahyre-storage-state.json")
    parser.add_argument("--artifacts", default="instahyre-application-artifacts")
    parser.add_argument("--limit", type=int, default=100)
    parser.add_argument("--candidate-years", type=int, default=6)
    parser.add_argument("--max-search-pages", type=int, default=20)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--user-agent", default=INSTAHYRE_USER_AGENT)
    parser.add_argument("--capture-session", action="store_true")
    args = parser.parse_args()
    if not 1 <= args.limit <= 100:
        parser.error("--limit must be between 1 and 100")
    if args.capture_session:
        capture_instahyre_storage_state(Path(args.storage_state))
        return
    report = run_instahyre_applications(
        storage_state=Path(args.storage_state),
        output=Path(args.artifacts),
        limit=args.limit,
        candidate_years=args.candidate_years,
        max_search_pages=args.max_search_pages,
        dry_run=args.dry_run,
        user_agent=args.user_agent,
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
