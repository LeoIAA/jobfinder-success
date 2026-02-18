"""
TotalJobs scraper (Selenium, headless).

Uses slug-based search URLs (/jobs/product-manager, /jobs/product-owner)
which return properly filtered results, unlike the query-param search
which ignores keywords.

Requires: selenium, webdriver-manager
Note: TotalJobs is geo-restricted -- requires a UK VPN connection.

Data selectors use TotalJobs' data-at attributes which are stable
identifiers (not CSS class names which are hashed/randomized).
"""
import time
import re
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.service import Service
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
    InvalidSessionIdException,
)
from webdriver_manager.chrome import ChromeDriverManager

import config
from models import (
    JobListing,
    ExcludedJob,
    check_title_filter,
    check_description_filter,
    detect_work_type,
    deduplicate,
)


BASE_URL = "https://www.totaljobs.com"
RESULTS_PER_PAGE = 25
MAX_PAGES = 40  # safety cap per search slug

# TotalJobs slug-based search: query-param search returns unfiltered garbage
# These are derived from config.SEARCH_QUERIES
SEARCH_SLUGS = {
    "Product Manager": "product-manager",
    "Product Owner": "product-owner",
}


def _make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _is_session_alive(driver) -> bool:
    if driver is None:
        return False
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def _restart_driver(driver) -> webdriver.Chrome | None:
    try:
        driver.quit()
    except Exception:
        pass
    try:
        print("[TotalJobs]   -> Restarting browser...")
        return _make_driver()
    except Exception as e:
        print(f"[TotalJobs]   -> Could not restart browser: {e}")
        return None


def _dismiss_cookies(driver: webdriver.Chrome):
    """Accept or dismiss cookie banner if present."""
    try:
        cookie_btn = driver.find_element(
            By.CSS_SELECTOR,
            "#cookie-consent-accept, [data-cookie-accept], button[class*='cookie'], "
            "[id*='onetrust-accept'], button[title='Accept All']"
        )
        cookie_btn.click()
        time.sleep(0.5)
    except (NoSuchElementException, WebDriverException):
        pass


def _check_vpn(driver: webdriver.Chrome) -> bool:
    """Check if TotalJobs is reachable (requires UK VPN)."""
    try:
        driver.get(BASE_URL)
        time.sleep(3)
        # If we get redirected to a block page or the page doesn't load
        title = driver.title.lower()
        if "access denied" in title or "blocked" in title or "error" in title:
            return False
        # Check for job search elements on homepage
        try:
            driver.find_element(By.CSS_SELECTOR, '[data-at="searchbar-keyword-input"], input[name="Keywords"]')
            return True
        except NoSuchElementException:
            # Page loaded but may be geo-blocked or different layout
            page_text = driver.page_source.lower()
            if "totaljobs" in page_text:
                return True
            return False
    except Exception:
        return False


def _search_page_url(slug: str, page: int = 1) -> str:
    """Build a TotalJobs search URL using the slug format."""
    url = f"{BASE_URL}/jobs/{slug}"
    if page > 1:
        url += f"?page={page}"
    return url


def _scrape_search_results(driver: webdriver.Chrome, url: str) -> list[dict]:
    """
    Load a search results page and extract job data from cards.
    Uses TotalJobs' data-at attributes for stable selectors.
    """
    driver.get(url)
    time.sleep(2)
    _dismiss_cookies(driver)

    # Wait for job cards
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, '[data-at="job-item"]'))
        )
    except TimeoutException:
        # Check if "no jobs matching" message
        try:
            driver.find_element(By.XPATH, "//*[contains(text(), 'no job matching')]")
            print("[TotalJobs]   -> No results on page")
        except NoSuchElementException:
            print("[TotalJobs]   -> No job cards found (timeout)")
        return []

    cards = driver.find_elements(By.CSS_SELECTOR, '[data-at="job-item"]')
    results = []

    for card in cards:
        try:
            # Title + URL from the title link
            title = ""
            href = ""
            try:
                title_el = card.find_element(By.CSS_SELECTOR, '[data-at="job-item-title"]')
                title = title_el.text.strip()
                href = title_el.get_attribute("href") or ""
            except NoSuchElementException:
                # Fallback: any link with /job/ in href
                try:
                    link = card.find_element(By.CSS_SELECTOR, 'a[href*="/job/"]')
                    title = link.text.strip()
                    href = link.get_attribute("href") or ""
                except NoSuchElementException:
                    continue

            if not title:
                continue

            # Extract job ID from URL slug (e.g., "company-job106740634" -> "106740634")
            job_id = ""
            id_match = re.search(r'job(\d+)', href)
            if id_match:
                job_id = id_match.group(1)

            # Company
            company = ""
            try:
                company_el = card.find_element(By.CSS_SELECTOR, '[data-at="job-item-company-name"]')
                company = company_el.text.strip()
            except NoSuchElementException:
                pass

            # Location
            location = ""
            try:
                loc_el = card.find_element(By.CSS_SELECTOR, '[data-at="job-item-location"]')
                location = loc_el.text.strip()
            except NoSuchElementException:
                pass

            # Salary -- uses a hashed class but is a sibling span after location
            salary = ""
            try:
                # The salary span doesn't have a data-at, but it's the last metadata span
                # Try to find it by checking spans that contain £ or "per annum" / "per hour"
                spans = card.find_elements(By.CSS_SELECTOR, "span")
                for span in spans:
                    text = span.text.strip()
                    if text and ("£" in text or "per annum" in text.lower() or "per hour" in text.lower()
                                 or "competitive" in text.lower() or "negotiable" in text.lower()):
                        salary = text
                        break
            except Exception:
                pass

            # Date posted
            date_posted = ""
            try:
                time_el = card.find_element(By.CSS_SELECTOR, '[data-at="job-item-timeago"]')
                date_posted = time_el.text.strip()
            except NoSuchElementException:
                pass

            # Snippet for pre-filtering
            snippet = ""
            try:
                mid_el = card.find_element(By.CSS_SELECTOR, '[data-at="job-item-middle"]')
                snippet = mid_el.text.strip()
            except NoSuchElementException:
                pass

            full_url = href if href.startswith("http") else f"{BASE_URL}{href}"

            results.append({
                "id": job_id or href,  # fallback to href as ID if no numeric ID
                "title": title,
                "company": company,
                "location": location,
                "salary": salary,
                "date_posted": date_posted,
                "snippet": snippet,
                "url": full_url,
            })
        except Exception as e:
            print(f"[TotalJobs]   -> Error parsing card: {e}")
            continue

    return results


def _get_description(driver: webdriver.Chrome, url: str) -> dict:
    """
    Fetch a job detail page and extract description + metadata.
    Returns dict with description, salary, date_posted, work_type.
    """
    result = {"description": "", "salary": "", "date_posted": "", "work_type": ""}
    try:
        driver.get(url)
        time.sleep(1.5)

        # Description
        for selector in [
            '[data-at="section-text-jobDescription-content"]',
            '[data-at="job-ad-content"]',
            '.job-description',
            '#job-description',
        ]:
            try:
                desc_el = WebDriverWait(driver, 6).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                text = desc_el.text.strip()
                if not text:
                    # Headless fallback: innerHTML -> strip tags
                    html = desc_el.get_attribute("innerHTML") or ""
                    text = re.sub(r"<[^>]+>", " ", html).strip()
                if text and len(text) > 50:
                    result["description"] = text
                    break
            except (TimeoutException, NoSuchElementException):
                continue

        # Salary from detail page (may be more detailed than search card)
        try:
            sal_el = driver.find_element(By.CSS_SELECTOR, '[data-at="metadata-salary"]')
            sal_text = sal_el.text.strip()
            if sal_text:
                result["salary"] = sal_text
        except NoSuchElementException:
            pass

        # Date posted
        try:
            date_el = driver.find_element(By.CSS_SELECTOR, '[data-at="metadata-online-date"]')
            date_text = date_el.text.strip()
            if date_text:
                # Clean "Published: 2 days ago" -> "2 days ago"
                date_text = re.sub(r'^published:\s*', '', date_text, flags=re.IGNORECASE)
                result["date_posted"] = date_text
        except NoSuchElementException:
            pass

        # Work type (e.g., "Full Time", "Part Time", "Contract")
        try:
            wt_el = driver.find_element(By.CSS_SELECTOR, '[data-at="metadata-work-type"]')
            result["work_type"] = wt_el.text.strip()
        except NoSuchElementException:
            pass

    except TimeoutException:
        print(f"[TotalJobs]   -> Timeout loading {url}")
    except Exception as e:
        print(f"[TotalJobs]   -> Error fetching detail: {e}")

    return result


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def scrape_totaljobs(known_urls: set[str] = None) -> tuple[list[JobListing], list[ExcludedJob]]:
    """Run all TotalJobs searches. Returns (accepted_listings, excluded_listings)."""
    print("[TotalJobs] Starting scraper...")
    _known = set(u.lower().strip() for u in (known_urls or set()))

    try:
        driver = _make_driver()
    except Exception as e:
        print(f"[TotalJobs] ERROR: Could not start Chrome driver: {e}")
        return [], []

    raw_results = []
    seen_ids = set()
    excluded = []
    listings = []

    try:
        # VPN check
        print("[TotalJobs] Checking site accessibility (VPN required)...")
        if not _check_vpn(driver):
            print("[TotalJobs] ERROR: Cannot reach TotalJobs. Is your UK VPN active?")
            return [], []
        print("[TotalJobs] -> Site reachable")

        # --- Phase 1: collect search results ---
        # Use slug-based URLs derived from search queries
        for query in config.SEARCH_QUERIES:
            if driver is None:
                break

            slug = SEARCH_SLUGS.get(query)
            if not slug:
                # Generate slug from query: "Product Manager" -> "product-manager"
                slug = query.lower().replace(" ", "-")

            print(f"[TotalJobs] Searching: '{query}' (slug: /jobs/{slug})...")

            consecutive_empty = 0
            low_yield_count = 0

            for page in range(1, MAX_PAGES + 1):
                url = _search_page_url(slug, page)

                try:
                    results = _scrape_search_results(driver, url)
                except (WebDriverException, InvalidSessionIdException) as e:
                    print(f"[TotalJobs]   -> Error on page {page}: {e}")
                    driver = _restart_driver(driver)
                    if driver is None:
                        break
                    continue  # retry same page with fresh driver

                if not results:
                    consecutive_empty += 1
                    if consecutive_empty >= 2:
                        print(f"[TotalJobs]   -> No results on 2 consecutive pages, moving on")
                        break
                    continue
                else:
                    consecutive_empty = 0

                new_count = 0
                for r in results:
                    rid = r["id"]
                    if rid not in seen_ids:
                        seen_ids.add(rid)
                        raw_results.append(r)
                        new_count += 1

                print(
                    f"[TotalJobs]   -> Page {page}: {len(results)} results "
                    f"({new_count} new, {len(seen_ids)} unique total)"
                )

                # Early stop: 3 pages with 0 new results
                if new_count == 0:
                    low_yield_count += 1
                    if low_yield_count >= 3:
                        print(f"[TotalJobs]   -> 3 pages with 0 new results, moving on")
                        break
                else:
                    low_yield_count = 0

                if len(results) < RESULTS_PER_PAGE:
                    break

                time.sleep(1)

        print(f"[TotalJobs] Total unique raw results: {len(raw_results)}")

        # --- Phase 2: title filter + snippet pre-filter ---
        needs_detail = []

        for r in raw_results:
            title = r["title"]
            company = r["company"]
            location = r["location"]
            url = r["url"]

            passes, reason = check_title_filter(title)
            if not passes:
                excluded.append(ExcludedJob("TotalJobs", title, company, location, url, reason))
                continue

            # Snippet pre-filter (saves detail page fetches)
            snippet = r.get("snippet", "")
            if snippet:
                passes, reason = check_description_filter(snippet)
                if not passes:
                    excluded.append(ExcludedJob("TotalJobs", title, company, location, url, reason))
                    continue

            needs_detail.append(r)

        # Skip jobs already in spreadsheet
        if _known:
            before = len(needs_detail)
            needs_detail = [r for r in needs_detail if r["url"].lower().strip() not in _known]
            skipped = before - len(needs_detail)
            if skipped:
                print(f"[TotalJobs] Skipped {skipped} already in spreadsheet")

        print(
            f"[TotalJobs] After title/snippet filter: {len(needs_detail)} to fetch, "
            f"{len(excluded)} excluded"
        )

        # --- Phase 3: fetch detail pages ---
        if needs_detail and driver is not None:
            print(f"[TotalJobs] Fetching {len(needs_detail)} detail pages...")

            for i, r in enumerate(needs_detail):
                title = r["title"]
                company = r["company"]
                location_text = r["location"]
                url = r["url"]
                card_salary = r.get("salary", "")
                card_date = r.get("date_posted", "")

                # Check browser health
                if not _is_session_alive(driver):
                    driver = _restart_driver(driver)
                    if driver is None:
                        print("[TotalJobs]   -> Browser dead, stopping detail fetch")
                        break

                try:
                    detail = _get_description(driver, url)
                except (WebDriverException, InvalidSessionIdException):
                    driver = _restart_driver(driver)
                    if driver is None:
                        detail = {"description": "", "salary": "", "date_posted": "", "work_type": ""}
                    else:
                        try:
                            detail = _get_description(driver, url)
                        except Exception:
                            detail = {"description": "", "salary": "", "date_posted": "", "work_type": ""}

                description = detail.get("description", "")
                salary = detail.get("salary", "") or card_salary
                date_posted = detail.get("date_posted", "") or card_date

                # Full description filter
                if description:
                    passes, reason = check_description_filter(description)
                    if not passes:
                        excluded.append(ExcludedJob("TotalJobs", title, company, location_text, url, reason))
                        continue

                work_type = detect_work_type(title, location_text, description)

                listings.append(JobListing(
                    source="TotalJobs",
                    title=title,
                    company=company,
                    location=location_text,
                    salary=salary,
                    url=url,
                    description=description,
                    date_posted=date_posted,
                    work_type=work_type,
                ))

                if (i + 1) % 20 == 0:
                    print(f"[TotalJobs]   Fetched {i + 1}/{len(needs_detail)}...")
                time.sleep(0.5)

    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass

    print(f"[TotalJobs] Accepted: {len(listings)}, Excluded: {len(excluded)}")
    return deduplicate(listings), excluded


if __name__ == "__main__":
    jobs, excl = scrape_totaljobs()
    for j in jobs[:5]:
        print(f"  -> {j.title} @ {j.company} -- {j.work_type}")
    print(f"\nAccepted: {len(jobs)}, Excluded: {len(excl)}")
