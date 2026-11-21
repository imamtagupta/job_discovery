import argparse
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any


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
        input("Log in to Instahyre in the browser, then press Enter here to save the session: ")
        if page.locator('a[href="/logout/"]').count() == 0:
            browser.close()
            raise RuntimeError("Instahyre login was not detected; storage state was not saved")
        context.storage_state(path=str(output))
        browser.close()


def run_instahyre_applications(
    *,
    storage_state: Path,
    output: Path,
    limit: int = 100,
    candidate_years: int = 6,
    max_search_pages: int = 20,
    dry_run: bool = False,
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
        context = browser.new_context(storage_state=str(storage_state))
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

        search = page.get_by_role("textbox", name="Enter skills or title, e.g Java, C++")
        search.fill("Python")
        page.get_by_role("button", name="Search").evaluate("element => element.click()")
        page.wait_for_timeout(1_500)

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
                response = page.request.get(job["url"])
                html = response.text()
                experience = _experience_from_html(html)
                if "python" not in html.casefold():
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
            search = page.get_by_role("textbox", name="Enter skills or title, e.g Java, C++")
            search.fill("Python")
            page.get_by_role("button", name="Search").evaluate("element => element.click()")
            page.wait_for_timeout(1_500)

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
    )
    print(json.dumps(report, indent=2, sort_keys=True))


if __name__ == "__main__":
    main()
