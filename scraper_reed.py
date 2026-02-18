"""
Reed.co.uk API scraper.
Returns both accepted and excluded listings (with exclusion reasons).
"""
import time
import requests
from requests.auth import HTTPBasicAuth

import config
from models import (
    JobListing,
    ExcludedJob,
    check_title_filter,
    check_description_filter,
    detect_work_type,
    format_salary,
    clean_html,
    deduplicate,
)


def _auth() -> HTTPBasicAuth:
    return HTTPBasicAuth(config.REED_API_KEY, "")


def search_reed(keyword: str, location: str) -> list[dict]:
    params = {
        "keywords": keyword,
        "locationName": location,
        "resultsToTake": config.REED_RESULTS_PER_PAGE,
        "resultsToSkip": 0,
    }
    all_results = []
    while True:
        resp = requests.get(
            f"{config.REED_BASE_URL}/search",
            params=params,
            auth=_auth(),
            timeout=30,
        )
        resp.raise_for_status()
        data = resp.json()
        results = data.get("results", data) if isinstance(data, dict) else data
        if not results:
            break
        all_results.extend(results)
        if len(results) < config.REED_RESULTS_PER_PAGE:
            break
        params["resultsToSkip"] += config.REED_RESULTS_PER_PAGE
        time.sleep(0.5)
    return all_results


def get_job_details(job_id: int) -> dict:
    resp = requests.get(
        f"{config.REED_BASE_URL}/jobs/{job_id}",
        auth=_auth(),
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def scrape_reed(known_urls: set[str] = None) -> tuple[list[JobListing], list[ExcludedJob]]:
    """Run all searches. Returns (accepted_listings, excluded_listings)."""
    if not config.REED_API_KEY:
        print("[Reed] ERROR: No API key set.")
        return [], []

    _known = set(u.lower().strip() for u in (known_urls or set()))

    raw_results = []
    seen_ids = set()

    for keyword in config.SEARCH_QUERIES:
        for location in config.SEARCH_LOCATIONS:
            print(f"[Reed] Searching: '{keyword}' in '{location}'...")
            try:
                results = search_reed(keyword, location)
                for r in results:
                    jid = r.get("jobId")
                    if jid not in seen_ids:
                        seen_ids.add(jid)
                        raw_results.append(r)
                print(f"[Reed]   â†’ {len(results)} results ({len(seen_ids)} unique so far)")
            except requests.RequestException as e:
                print(f"[Reed]   â†’ Error: {e}")
            time.sleep(0.3)

    print(f"[Reed] Total unique raw results: {len(raw_results)}")

    listings = []
    excluded = []

    for i, r in enumerate(raw_results):
        job_id = r.get("jobId")
        title = r.get("jobTitle", "")
        company = r.get("employerName", "")
        location = r.get("locationName", "")
        # Use the jobUrl from search results (has proper slug)
        url = r.get("jobUrl", f"https://www.reed.co.uk/jobs/{job_id}")

        # Title filter
        passes, reason = check_title_filter(title)
        if not passes:
            excluded.append(ExcludedJob("Reed", title, company, location, url, reason))
            continue

        # Skip if already in spreadsheet
        if url.lower().strip() in _known:
            continue

        # Fetch full details
        min_sal = r.get("minimumSalary")
        max_sal = r.get("maximumSalary")
        date_posted = r.get("date", "")

        try:
            details = get_job_details(job_id)
            description = clean_html(details.get("jobDescription", ""))
            yearly_min = details.get("yearlyMinimumSalary", min_sal)
            yearly_max = details.get("yearlyMaximumSalary", max_sal)
            # Prefer the detail URL if available
            detail_url = details.get("jobUrl") or details.get("externalUrl", "")
            if detail_url:
                url = detail_url
        except requests.RequestException as e:
            print(f"[Reed]   â†’ Could not fetch details for job {job_id}: {e}")
            description = clean_html(r.get("jobDescription", ""))
            yearly_min, yearly_max = min_sal, max_sal

        # Description filter
        passes, reason = check_description_filter(description)
        if not passes:
            excluded.append(ExcludedJob("Reed", title, company, location, url, reason))
            continue

        salary_str = format_salary(yearly_min, yearly_max)
        work_type = detect_work_type(title, location, description)

        listings.append(JobListing(
            source="Reed",
            title=title,
            company=company,
            location=location,
            salary=salary_str,
            url=url,
            description=description,
            date_posted=date_posted[:10] if date_posted else "",
            work_type=work_type,
        ))

        if (i + 1) % 20 == 0:
            print(f"[Reed]   Processed {i+1}/{len(raw_results)}...")
        time.sleep(0.2)

    print(f"[Reed] Accepted: {len(listings)}, Excluded: {len(excluded)}")
    return deduplicate(listings), excluded


if __name__ == "__main__":
    jobs, excl = scrape_reed()
    for j in jobs[:5]:
        print(f"  âœ“ {j.title} @ {j.company} â€” {j.work_type}")
    print(f"\nAccepted: {len(jobs)}, Excluded: {len(excl)}")
