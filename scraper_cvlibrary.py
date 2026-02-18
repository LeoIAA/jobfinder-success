"""
CV-Library scraper (Selenium throughout).

Uses Selenium for both search pages and detail pages to avoid 403s
from CV-Library's Cloudflare protection on plain requests.

Requires: selenium, webdriver-manager
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


BASE_URL = "https://www.cv-library.co.uk"
RESULTS_PER_PAGE = 25
MAX_PAGES = 5  # safety cap per search


def _make_driver() -> webdriver.Chrome:
    opts = Options()
    opts.add_argument("--headless=new")
    opts.add_argument("--no-sandbox")
    opts.add_argument("--disable-dev-shm-usage")
    opts.add_argument("--disable-gpu")
    opts.add_argument("--window-size=1920,1080")
    opts.add_argument(
        "user-agent=Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    )
    service = Service(ChromeDriverManager().install())
    return webdriver.Chrome(service=service, options=opts)


def _is_session_alive(driver) -> bool:
    """Check if the Selenium session is still valid."""
    if driver is None:
        return False
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def _restart_driver(driver) -> webdriver.Chrome | None:
    """Quit old driver and start a fresh one. Returns None on failure."""
    try:
        driver.quit()
    except Exception:
        pass
    try:
        print("[CV-Library]   -> Restarting browser...")
        return _make_driver()
    except Exception as e:
        print(f"[CV-Library]   -> Could not restart browser: {e}")
        return None


def _search_page_url(keyword: str, location: str, page: int = 1) -> str:
    params = f"q={quote_plus(keyword)}&geo={quote_plus(location)}&distance=30"
    if page > 1:
        params += f"&page={page}"
    return f"{BASE_URL}/search-jobs?{params}"


def _dismiss_cookies(driver: webdriver.Chrome):
    """Accept cookie banner if present."""
    try:
        cookie_btn = driver.find_element(
            By.CSS_SELECTOR,
            "#cookie-consent-accept, [data-cookie-accept], button[class*='cookie']",
        )
        cookie_btn.click()
        time.sleep(0.5)
    except (NoSuchElementException, WebDriverException):
        pass


def _scrape_search_results(driver: webdriver.Chrome, url: str) -> list[dict]:
    """
    Load a search results page and extract job data from article.job elements.
    Also grabs the search-page snippet for pre-filtering.
    """
    driver.get(url)
    time.sleep(2)
    _dismiss_cookies(driver)

    # Wait for job cards
    try:
        WebDriverWait(driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, "article.job[data-job-id]"))
        )
    except TimeoutException:
        print("[CV-Library]   -> No job cards found on page")
        return []

    cards = driver.find_elements(By.CSS_SELECTOR, "article.job[data-job-id]")
    results = []

    for card in cards:
        try:
            job_id = card.get_attribute("data-job-id")
            title = card.get_attribute("data-job-title") or ""
            company = (card.get_attribute("data-company-name") or "").strip()
            location = card.get_attribute("data-job-location") or ""
            salary = card.get_attribute("data-job-salary") or ""
            posted = card.get_attribute("data-job-posted") or ""

            # Grab snippet text for pre-filtering
            try:
                snippet_el = card.find_element(By.CSS_SELECTOR, ".job__description")
                snippet = snippet_el.text.strip()
            except NoSuchElementException:
                snippet = ""

            # Get the link path
            try:
                link_el = card.find_element(By.CSS_SELECTOR, ".job__title a")
                path = link_el.get_attribute("href") or ""
            except NoSuchElementException:
                path = f"/job/{job_id}"

            results.append({
                "id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "salary": salary,
                "posted": posted,
                "snippet": snippet,
                "url": path if path.startswith("http") else f"{BASE_URL}{path}",
            })
        except Exception as e:
            print(f"[CV-Library]   -> Error parsing card: {e}")
            continue

    return results


def _get_description_selenium(driver: webdriver.Chrome, url: str) -> str:
    """Fetch a job detail page with Selenium and extract the description."""
    import re as _re
    try:
        driver.get(url)
        time.sleep(2)
        _dismiss_cookies(driver)

        # Phase 1: JS extraction — fastest, handles hidden/dynamic content
        try:
            text = driver.execute_script("""
                var selectors = [
                    '.job__description',
                    '[class*="job__description"]',
                    '.job-description',
                    '#job-description',
                    '[class*="JobDescription"]',
                    '[class*="jobDescription"]',
                    '[class*="description__content"]',
                    '[data-testid="job-description"]',
                    'article .description',
                    '.vacancy-description',
                    '.job-detail__description',
                    '.job-content',
                ];
                for (var i = 0; i < selectors.length; i++) {
                    var el = document.querySelector(selectors[i]);
                    if (el) {
                        var text = el.innerText || '';
                        if (text.trim().length > 50) return text.trim();
                        var html = el.innerHTML || '';
                        text = html.replace(/<[^>]+>/g, ' ').replace(/\\s+/g, ' ').trim();
                        if (text.length > 50) return text;
                    }
                }
                // Broader fallback: find the largest text block in content area
                var candidates = document.querySelectorAll(
                    'section, article, [role="main"], main, .content, .detail, [class*="detail"]'
                );
                var best = '';
                for (var i = 0; i < candidates.length; i++) {
                    var t = candidates[i].innerText || '';
                    if (t.length > best.length && t.length > 200) best = t;
                }
                if (best.length > 200) return best.trim();
                return '';
            """)
            if text and len(text) > 50:
                return text
        except Exception:
            pass

        # Phase 2: Selenium element-by-element with expanded selectors
        selectors = [
            ".job__description",
            "[class*='job__description']",
            ".job-description",
            "#job-description",
            "[class*='JobDescription']",
            "[class*='jobDescription']",
            "[class*='description__content']",
            "[data-testid='job-description']",
            "article .description",
            ".vacancy-description",
            ".job-detail__description",
            ".job-content",
        ]
        for selector in selectors:
            try:
                desc_el = WebDriverWait(driver, 4).until(
                    EC.presence_of_element_located((By.CSS_SELECTOR, selector))
                )
                text = desc_el.text.strip()
                if not text:
                    html = desc_el.get_attribute("innerHTML") or ""
                    text = _re.sub(r"<[^>]+>", " ", html).strip()
                if text and len(text) > 50:
                    return text
            except (TimeoutException, NoSuchElementException):
                continue

        # Phase 3: page source regex — look for description in raw HTML
        try:
            page_source = driver.page_source
            match = _re.search(
                r'(?:class|id)=["\'][^"\']*(?:description|job-content|jobDescription)[^"\']*["\']'
                r'[^>]*>(.*?)</(?:div|section|article)',
                page_source,
                _re.DOTALL | _re.IGNORECASE,
            )
            if match:
                text = _re.sub(r"<[^>]+>", " ", match.group(1)).strip()
                text = _re.sub(r"\\s+", " ", text)
                if len(text) > 100:
                    return text
        except Exception:
            pass

        print(f"[CV-Library]   -> No description found at {url}")
        return ""
    except (TimeoutException, NoSuchElementException) as e:
        print(f"[CV-Library]   -> No description found at {url}")


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def scrape_cvlibrary(known_urls: set[str] = None) -> tuple[list[JobListing], list[ExcludedJob]]:
    """Run all CV-Library searches. Returns (accepted_listings, excluded_listings)."""
    print("[CV-Library] Starting scraper...")
    _known = set(u.lower().strip() for u in (known_urls or set()))

    try:
        driver = _make_driver()
    except Exception as e:
        print(f"[CV-Library] ERROR: Could not start Chrome driver: {e}")
        return [], []

    raw_results = []
    seen_ids = set()
    excluded = []
    listings = []

    try:
        # --- Phase 1: collect search results ---
        for keyword in config.SEARCH_QUERIES:
            if driver is None:
                break
            for location in config.SEARCH_LOCATIONS:
                if driver is None:
                    break
                print(f"[CV-Library] Searching: '{keyword}' in '{location}'...")
                page = 1

                while page <= MAX_PAGES:
                    url = _search_page_url(keyword, location, page)
                    try:
                        results = _scrape_search_results(driver, url)
                    except (WebDriverException, InvalidSessionIdException) as e:
                        print(f"[CV-Library]   -> Error on page {page}: {e}")
                        driver = _restart_driver(driver)
                        if driver is None:
                            break
                        continue  # retry same page with fresh driver

                    if not results:
                        break

                    new_count = 0
                    for r in results:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            raw_results.append(r)
                            new_count += 1

                    print(
                        f"[CV-Library]   -> Page {page}: {len(results)} results "
                        f"({new_count} new, {len(seen_ids)} unique total)"
                    )

                    if len(results) < RESULTS_PER_PAGE:
                        break

                    page += 1
                    time.sleep(1)

        print(f"[CV-Library] Total unique raw results: {len(raw_results)}")

        # --- Phase 2: title filter + snippet pre-filter ---
        needs_detail = []

        for r in raw_results:
            title = r["title"]
            company = r["company"]
            location = r["location"]
            url = r["url"]

            passes, reason = check_title_filter(title)
            if not passes:
                excluded.append(ExcludedJob("CV-Library", title, company, location, url, reason))
                continue

            snippet = r.get("snippet", "")
            if snippet:
                passes, reason = check_description_filter(snippet)
                if not passes:
                    excluded.append(ExcludedJob("CV-Library", title, company, location, url, reason))
                    continue

            needs_detail.append(r)

        # Skip jobs already in spreadsheet
        if _known:
            before = len(needs_detail)
            needs_detail = [r for r in needs_detail if r["url"].lower().strip() not in _known]
            skipped = before - len(needs_detail)
            if skipped:
                print(f"[CV-Library] Skipped {skipped} already in spreadsheet")

        print(
            f"[CV-Library] After title/snippet filter: {len(needs_detail)} to fetch, "
            f"{len(excluded)} excluded"
        )

        # --- Phase 3: fetch details + build listings (Selenium, sequential) ---
        if needs_detail and driver is not None:
            print(f"[CV-Library] Fetching {len(needs_detail)} detail pages...")

            for i, r in enumerate(needs_detail):
                title = r["title"]
                company = r["company"]
                location = r["location"]
                url = r["url"]
                salary = r["salary"].strip() if r["salary"] else ""
                posted = r["posted"][:10] if r["posted"] else ""

                # Fetch description
                if not _is_session_alive(driver):
                    driver = _restart_driver(driver)
                    if driver is None:
                        print("[CV-Library]   -> Browser dead, using remaining jobs without descriptions")
                        break

                try:
                    description = _get_description_selenium(driver, url)
                except (WebDriverException, InvalidSessionIdException):
                    driver = _restart_driver(driver)
                    if driver is None:
                        description = ""
                    else:
                        try:
                            description = _get_description_selenium(driver, url)
                        except Exception:
                            description = ""

                # Full description filter
                if description:
                    passes, reason = check_description_filter(description)
                    if not passes:
                        excluded.append(ExcludedJob("CV-Library", title, company, location, url, reason))
                        continue

                work_type = detect_work_type(title, location, description)

                listings.append(JobListing(
                    source="CV-Library",
                    title=title,
                    company=company,
                    location=location,
                    salary=salary,
                    url=url,
                    description=description,
                    date_posted=posted,
                    work_type=work_type,
                ))

                if (i + 1) % 20 == 0:
                    print(f"[CV-Library]   Fetched {i+1}/{len(needs_detail)}...")
                time.sleep(0.3)

    finally:
        try:
            if driver:
                driver.quit()
        except Exception:
            pass

    print(f"[CV-Library] Accepted: {len(listings)}, Excluded: {len(excluded)}")
    return deduplicate(listings), excluded


if __name__ == "__main__":
    jobs, excl = scrape_cvlibrary()
    for j in jobs[:5]:
        print(f"  OK {j.title} @ {j.company} -- {j.work_type}")
    print(f"\nAccepted: {len(jobs)}, Excluded: {len(excl)}")
