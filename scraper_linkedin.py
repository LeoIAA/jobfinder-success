"""
LinkedIn scraper (Selenium with cookies copied from Chrome profile).

Copies LinkedIn session cookies from an existing Chrome profile into a
lightweight temp profile, avoiding profile lock issues.

Anti-detection:
  - Automation flags disabled
  - Random delays between actions
  - Random scroll behaviour
  - Captcha/security-check detection with pause

Optimization:
  - Batch tab opening for detail pages (7 tabs at a time)
  - Parallel page loading reduces per-job wait time
"""
import os
import time
import random
import re
from urllib.parse import quote_plus

from selenium import webdriver
from selenium.webdriver.chrome.options import Options
from selenium.webdriver.common.by import By
from selenium.webdriver.common.action_chains import ActionChains
from selenium.webdriver.support.ui import WebDriverWait
from selenium.webdriver.support import expected_conditions as EC
from selenium.common.exceptions import (
    TimeoutException,
    NoSuchElementException,
    WebDriverException,
    StaleElementReferenceException,
    InvalidSessionIdException,
)

import config
from models import (
    JobListing,
    ExcludedJob,
    check_title_filter,
    check_description_filter,
    detect_work_type,
    deduplicate,
)


LINKEDIN_BASE = "https://www.linkedin.com"
MAX_PAGES = 69           # standard mode: stop well before LinkedIn's 1000-result cap
MAX_PAGES_EXTENDED = 140  # extended mode: go as deep as possible
RESULTS_PER_PAGE = 25
BATCH_SIZE = 7  # number of tabs to open in parallel for detail fetching

# User-agent pool
USER_AGENTS = [
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/123.0.0.0 Safari/537.36",
    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
]


def _random_delay(min_s: float = 2.0, max_s: float = 6.0):
    """Sleep a random duration to mimic human behaviour."""
    time.sleep(random.uniform(min_s, max_s))


def _short_delay():
    """Short delay for minor actions."""
    time.sleep(random.uniform(0.5, 1.5))


def _make_driver() -> webdriver.Chrome:
    """Launch Chrome via subprocess with debugging, then connect Selenium."""
    import subprocess

    chrome_data_dir = getattr(config, "LINKEDIN_CHROME_DATA_DIR", "/tmp/chrome-linkedin")

    # Launch Chrome with remote debugging
    proc = subprocess.Popen([
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "--remote-debugging-port=9222",
        f"--user-data-dir={chrome_data_dir}",
        "--disable-blink-features=AutomationControlled",
        "--no-first-run",
        "--no-default-browser-check",
        "--disable-popup-blocking",
        "--window-size=1920,1080",
    ], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    print("[LinkedIn]   -> Chrome launched, connecting...")
    time.sleep(5)

    opts = Options()
    opts.debugger_address = "127.0.0.1:9222"

    driver = webdriver.Chrome(options=opts)

    # Remove webdriver flag from navigator
    driver.execute_cdp_cmd(
        "Page.addScriptToEvaluateOnNewDocument",
        {"source": "Object.defineProperty(navigator, 'webdriver', {get: () => undefined})"},
    )

    # Store process for cleanup
    driver._chrome_proc = proc

    print("[LinkedIn]   -> Driver ready")
    return driver


def _is_session_alive(driver) -> bool:
    if driver is None:
        return False
    try:
        _ = driver.current_url
        return True
    except Exception:
        return False


def _restart_driver(driver):
    try:
        proc = getattr(driver, "_chrome_proc", None)
        driver.quit()
        if proc:
            proc.terminate()
            proc.wait(timeout=5)
    except Exception:
        pass
    try:
        print("[LinkedIn]   -> Restarting browser...")
        return _make_driver()
    except Exception as e:
        print(f"[LinkedIn]   -> Could not restart browser: {e}")
        return None


def _random_mouse_move(driver):
    """Perform a small random mouse movement for anti-detection."""
    try:
        action = ActionChains(driver)
        x_offset = random.randint(-100, 100)
        y_offset = random.randint(-50, 50)
        action.move_by_offset(x_offset, y_offset).perform()
        action.move_by_offset(-x_offset, -y_offset).perform()
    except Exception:
        pass


def _human_scroll(driver, scrolls: int = 3):
    """Scroll down in a human-like pattern to load lazy content."""
    for _ in range(scrolls):
        scroll_amount = random.randint(300, 700)
        driver.execute_script(f"window.scrollBy(0, {scroll_amount});")
        time.sleep(random.uniform(0.4, 1.2))
    time.sleep(random.uniform(0.5, 1.0))


def _is_valid_date_text(text: str) -> bool:
    """Check if a string looks like a real date/time-ago value, not 'Promoted' etc."""
    if not text:
        return False
    t = text.lower().strip()
    if re.search(r"\d+\s*(second|minute|hour|day|week|month|year)s?\s*ago", t):
        return True
    if re.search(r"reposted\s+\d+", t):
        return True
    if re.search(r"\d{4}-\d{2}-\d{2}", t):
        return True
    if "just now" in t or "today" in t or "yesterday" in t:
        return True
    return False


def _check_security_wall(driver) -> bool:
    """Check if LinkedIn is showing a security check / captcha page."""
    page_source = driver.page_source.lower()
    indicators = [
        "security verification",
        "let's do a quick security check",
        "please verify you are a human",
        "challenge-form",
        "/checkpoint/challenge",
        "authwall",
    ]
    url = driver.current_url.lower()
    if "/checkpoint/" in url or "/authwall" in url:
        return True
    return any(ind in page_source for ind in indicators)


def _wait_for_security(driver, max_wait: int = 120):
    """If a security wall is detected, pause and wait for manual resolution."""
    if not _check_security_wall(driver):
        return False
    print("[LinkedIn]   !! Security check detected! Waiting for manual resolution...")
    print(f"[LinkedIn]      (Will wait up to {max_wait}s, resolve it in the browser window)")
    start = time.time()
    while time.time() - start < max_wait:
        time.sleep(5)
        if not _check_security_wall(driver):
            print("[LinkedIn]   -> Security check resolved, continuing...")
            _random_delay(2, 4)
            return True
    print("[LinkedIn]   -> Security check not resolved in time, skipping...")
    return False


def _check_login(driver) -> bool:
    """Verify we're logged into LinkedIn."""
    print("[LinkedIn]   -> Navigating to LinkedIn feed...")
    driver.get(f"{LINKEDIN_BASE}/feed/")
    print("[LinkedIn]   -> Page loaded, waiting...")
    _random_delay(3, 5)
    if _check_security_wall(driver):
        _wait_for_security(driver)
    current = driver.current_url.lower()
    if "/login" in current or "/authwall" in current or "/signup" in current:
        return False
    if "/feed" in current:
        return True
    title = driver.title.lower()
    if "log in" in title or "sign up" in title:
        return False
    return True


def _search_url(keyword: str, location: str, start: int = 0) -> str:
    """Build a LinkedIn job search URL."""
    params = f"keywords={quote_plus(keyword)}&location={quote_plus(location)}"
    if start > 0:
        params += f"&start={start}"
    return f"{LINKEDIN_BASE}/jobs/search/?{params}"


def _extract_job_cards(driver) -> list[dict]:
    """Extract job card data from the current search results page."""
    cards = []

    card_selectors = [
        "li.jobs-search-results__list-item",
        "li[data-occludable-job-id]",
        ".job-card-container",
        ".jobs-search-results-list li",
        "div.job-card-container--clickable",
    ]

    card_elements = []
    for selector in card_selectors:
        card_elements = driver.find_elements(By.CSS_SELECTOR, selector)
        if card_elements:
            break

    if not card_elements:
        card_elements = driver.find_elements(
            By.CSS_SELECTOR, "[data-job-id], [data-occludable-job-id]"
        )

    for card in card_elements:
        try:
            job_id = (
                card.get_attribute("data-occludable-job-id")
                or card.get_attribute("data-job-id")
                or ""
            ).strip()

            if not job_id:
                try:
                    inner = card.find_element(By.CSS_SELECTOR, "[data-job-id]")
                    job_id = inner.get_attribute("data-job-id") or ""
                except NoSuchElementException:
                    try:
                        link = card.find_element(By.CSS_SELECTOR, "a[href*='/jobs/view/']")
                        href = link.get_attribute("href") or ""
                        match = re.search(r"/jobs/view/(\d+)", href)
                        if match:
                            job_id = match.group(1)
                    except NoSuchElementException:
                        continue

            if not job_id:
                continue

            # Title
            title = ""
            for sel in [
                ".job-card-list__title",
                ".job-card-container__link",
                "a.job-card-list__title--link",
                ".artdeco-entity-lockup__title",
                "a[href*='/jobs/view/']",
            ]:
                try:
                    el = card.find_element(By.CSS_SELECTOR, sel)
                    title = el.text.strip()
                    if title:
                        break
                except NoSuchElementException:
                    continue

            # Company
            company = ""
            for sel in [
                ".job-card-container__primary-description",
                ".artdeco-entity-lockup__subtitle",
                ".job-card-container__company-name",
            ]:
                try:
                    el = card.find_element(By.CSS_SELECTOR, sel)
                    company = el.text.strip()
                    if company:
                        break
                except NoSuchElementException:
                    continue

            # Location
            location = ""
            for sel in [
                ".job-card-container__metadata-wrapper li",
                ".artdeco-entity-lockup__caption",
                ".job-card-container__metadata-item",
            ]:
                try:
                    el = card.find_element(By.CSS_SELECTOR, sel)
                    location = el.text.strip()
                    if location:
                        break
                except NoSuchElementException:
                    continue

            # Date posted - validate to avoid "Promoted", "Viewed" etc.
            date_posted = ""
            for sel in [
                ".job-card-container__listed-time",
                ".job-card-container__footer-item",
                "time",
            ]:
                try:
                    el = card.find_element(By.CSS_SELECTOR, sel)
                    candidate = el.text.strip()
                    if not candidate:
                        candidate = el.get_attribute("datetime") or ""
                    if _is_valid_date_text(candidate):
                        date_posted = candidate
                        break
                except NoSuchElementException:
                    continue

            url = f"{LINKEDIN_BASE}/jobs/view/{job_id}/"

            cards.append({
                "id": job_id,
                "title": title,
                "company": company,
                "location": location,
                "date_posted": date_posted,
                "url": url,
            })

        except StaleElementReferenceException:
            continue
        except Exception as e:
            print(f"[LinkedIn]   -> Error parsing card: {e}")
            continue

    return cards


def _scroll_job_list(driver):
    """Scroll the job list panel to load all results on the page."""
    list_selectors = [
        ".jobs-search-results-list",
        ".jobs-search__results-list",
        ".scaffold-layout__list",
    ]
    list_el = None
    for sel in list_selectors:
        try:
            list_el = driver.find_element(By.CSS_SELECTOR, sel)
            break
        except NoSuchElementException:
            continue

    if list_el:
        for i in range(4):
            driver.execute_script(
                "arguments[0].scrollTop += arguments[1];",
                list_el,
                random.randint(400, 700),
            )
            time.sleep(random.uniform(0.5, 1.0))
    else:
        _human_scroll(driver, scrolls=5)


def _extract_detail_from_tab(driver) -> dict:
    """
    Extract description, salary, date from the CURRENT tab's job detail page.
    Does NOT navigate -- assumes page is already loaded.
    Uses a single JS call for speed where possible.
    """
    result = {"title": "", "description": "", "salary": "", "date_posted": ""}

    if _check_security_wall(driver):
        resolved = _wait_for_security(driver)
        if not resolved:
            return result

    # Click "Show more" via JS — fast, no waiting for clickability
    try:
        driver.execute_script("""
            var btns = document.querySelectorAll(
                'button.jobs-description__footer-button, button[aria-label*="Show more"], button[aria-label*="See more"]'
            );
            if (btns.length > 0) btns[0].click();
        """)
        time.sleep(0.3)
    except Exception:
        pass

    # Extract everything in one JS call
    try:
        data = driver.execute_script("""
            var desc = '';
            var selectors = ['.jobs-description__content', '.jobs-description-content__text',
                             '.jobs-box__html-content', '#job-details', '.jobs-description'];
            for (var i = 0; i < selectors.length; i++) {
                var el = document.querySelector(selectors[i]);
                if (el) {
                    var text = el.innerText || '';
                    if (text.trim().length > 50) { desc = text.trim(); break; }
                    var html = el.innerHTML || '';
                    text = html.replace(/<[^>]+>/g, ' ').trim();
                    if (text.length > 50) { desc = text; break; }
                }
            }

            // Fallback: find "About the job" text node and extract content after it.
            // Robust against LinkedIn class name changes since it searches literal text.
            if (!desc) {
                var walker = document.createTreeWalker(document.body, NodeFilter.SHOW_TEXT, null, false);
                var found = false;
                while (walker.nextNode() && !found) {
                    if (walker.currentNode.textContent.trim().toLowerCase() === 'about the job') {
                        var node = walker.currentNode.parentElement;
                        while (node && node !== document.body) {
                            var txt = node.innerText || '';
                            if (txt.length > 200) {
                                var cut = txt.toLowerCase().indexOf('about the job');
                                if (cut >= 0) {
                                    desc = txt.slice(cut + 13).trim();
                                    found = true;
                                    break;
                                }
                            }
                            node = node.parentElement;
                        }
                    }
                }
            }

            var title = '';
            var titleSels = [
                'h1.job-details-jobs-unified-top-card__job-title',
                '.jobs-unified-top-card__job-title h1',
                'h1[class*="job-title"]',
                '.job-details-jobs-unified-top-card__job-title',
                'h1'];
            for (var i = 0; i < titleSels.length; i++) {
                var el = document.querySelector(titleSels[i]);
                if (el && el.textContent.trim()) { title = el.textContent.trim(); break; }
            }
            if (!title) {
                var t = document.title || '';
                var cut = t.lastIndexOf(' | ');
                if (cut > 0) t = t.slice(0, cut);
                if (t && t.toLowerCase() !== 'linkedin') title = t.trim();
            }

            var salary = '';
            var salSels = ['.jobs-unified-top-card__job-insight--highlight',
                           '.salary-main-rail__data-body', '.compensation__salary', "span[class*='salary']"];
            for (var i = 0; i < salSels.length; i++) {
                var el = document.querySelector(salSels[i]);
                if (el && el.innerText) {
                    var t = el.innerText.trim();
                    if (t.indexOf('£') >= 0 || t.toLowerCase().indexOf('salary') >= 0) {
                        salary = t; break;
                    }
                }
            }

            var datePosted = '';
            var dateSels = ['.jobs-unified-top-card__posted-date', 'span.tvm__text--neutral',
                            '.posted-time-ago__text', "span[class*='posted']"];
            for (var i = 0; i < dateSels.length; i++) {
                var el = document.querySelector(dateSels[i]);
                if (el && el.innerText && el.innerText.trim()) {
                    datePosted = el.innerText.trim(); break;
                }
            }

            // Fallback date: scan top card for "X ago"
            if (!datePosted) {
                var topSels = ['.jobs-unified-top-card__primary-description',
                               '.job-details-jobs-unified-top-card__primary-description-container'];
                for (var i = 0; i < topSels.length; i++) {
                    var el = document.querySelector(topSels[i]);
                    if (el && el.innerText) {
                        var m = el.innerText.match(/((?:reposted\\s+)?\\d+\\s*(?:second|minute|hour|day|week|month|year)s?\\s*ago)/i);
                        if (m) { datePosted = m[1].trim(); break; }
                    }
                }
            }

            return {title: title, desc: desc, salary: salary, datePosted: datePosted};
        """)

        if data:
            result["title"] = data.get("title", "") or ""
            desc = data.get("desc", "")
            if desc:
                if desc.lower().startswith("about the job"):
                    desc = desc[len("about the job"):].lstrip(" \n\r\t:-")
                result["description"] = desc
            result["salary"] = data.get("salary", "")
            date_text = data.get("datePosted", "")
            if _is_valid_date_text(date_text):
                result["date_posted"] = date_text
    except Exception as e:
        # Fallback to Selenium element-by-element if JS fails
        for sel in [".jobs-description__content", "#job-details", ".jobs-description", "main", "article"]:
            try:
                el = driver.find_element(By.CSS_SELECTOR, sel)
                text = el.text.strip()
                if text and len(text) > 50:
                    lower = text.lower()
                    idx = lower.find("about the job")
                    if idx >= 0:
                        text = text[idx + len("about the job"):].lstrip(" \n\r\t:-")
                    if text:
                        result["description"] = text
                        break
            except NoSuchElementException:
                continue

    # Selenium fallback for title if JS didn't get it
    if not result["title"]:
        try:
            result["title"] = driver.find_element(By.TAG_NAME, "h1").text.strip()
        except Exception:
            pass

    return result


def _fetch_details_batch(driver, batch: list[dict]) -> list[dict]:
    """
    Open a batch of job detail pages in parallel tabs, extract data, close tabs.
    Mimics a user Ctrl+clicking several interesting jobs from search results.

    Opens tabs in two waves with a breathing gap to avoid 429 rate limits.
    Returns list of dicts with keys: url, description, salary, date_posted
    """
    if not batch:
        return []

    original_handle = driver.current_window_handle
    results = []

    # Phase 1: Open tabs in two waves to spread the load
    # Wave 1: first half, Wave 2: second half, with a pause between
    mid = (len(batch) + 1) // 2  # e.g. 7 → 4+3
    for wave_idx, wave in enumerate([batch[:mid], batch[mid:]]):
        if not wave:
            continue
        for item in wave:
            try:
                driver.execute_script(f"window.open('{item['url']}', '_blank');")
                time.sleep(random.uniform(1.0, 1.5))
            except Exception as e:
                print(f"[LinkedIn]   -> Error opening tab: {e}")
        # Breathing gap between waves
        if wave_idx == 0 and len(batch) > mid:
            time.sleep(random.uniform(2.0, 3.5))

    # Give all tabs time to fully load
    _random_delay(4, 7)

    # Get all tab handles (original + new ones)
    all_handles = driver.window_handles
    new_handles = [h for h in all_handles if h != original_handle]

    # Phase 2: Visit each tab, extract data, close it
    # Randomize visit order slightly to seem more natural
    tab_order = list(range(len(new_handles)))
    if len(tab_order) > 2:
        # Swap a couple of positions randomly
        for _ in range(min(2, len(tab_order) - 1)):
            i = random.randint(0, len(tab_order) - 2)
            tab_order[i], tab_order[i + 1] = tab_order[i + 1], tab_order[i]

    extracted = {}
    for idx in tab_order:
        if idx >= len(new_handles):
            continue
        handle = new_handles[idx]
        try:
            driver.switch_to.window(handle)
            time.sleep(random.uniform(0.5, 1.2))

            # Check for 429 / rate limit page
            page_src = driver.page_source.lower()
            if "429" in driver.title or "too many requests" in page_src or "rate limit" in page_src:
                print("[LinkedIn]   -> 429 detected, backing off 30s...")
                time.sleep(30)
                driver.refresh()
                time.sleep(random.uniform(3, 5))

            current_url = driver.current_url
            detail = _extract_detail_from_tab(driver)
            extracted[current_url] = detail

            # Close tab
            driver.close()
            time.sleep(random.uniform(0.2, 0.5))
        except Exception as e:
            print(f"[LinkedIn]   -> Error extracting from tab: {e}")
            try:
                driver.close()
            except Exception:
                pass

    # Switch back to original tab
    try:
        driver.switch_to.window(original_handle)
    except Exception:
        pass

    # Match results back to batch items
    for item in batch:
        matched = None
        item_id = re.search(r"/jobs/view/(\d+)", item["url"])
        item_id_str = item_id.group(1) if item_id else ""

        for tab_url, detail in extracted.items():
            if item_id_str and item_id_str in tab_url:
                matched = detail
                break

        if matched is None:
            matched = {"description": "", "salary": "", "date_posted": ""}

        results.append({
            "url": item["url"],
            "description": matched.get("description", ""),
            "salary": matched.get("salary", ""),
            "date_posted": matched.get("date_posted", ""),
        })

    return results


def _scrape_search_page(driver, url: str) -> list[dict]:
    """Load a search results page and extract all job cards."""
    _random_mouse_move(driver)
    driver.get(url)
    _random_delay(2, 4)

    if _check_security_wall(driver):
        resolved = _wait_for_security(driver)
        if not resolved:
            return []

    try:
        WebDriverWait(driver, 15).until(
            EC.presence_of_element_located((
                By.CSS_SELECTOR,
                ".jobs-search-results-list, .jobs-search__results-list, [data-occludable-job-id]"
            ))
        )
    except TimeoutException:
        print("[LinkedIn]   -> No results found on page")
        try:
            no_results = driver.find_element(
                By.CSS_SELECTOR, ".jobs-search-no-results-banner, .jobs-search-results-list--empty"
            )
            if no_results:
                print("[LinkedIn]   -> LinkedIn reports no matching jobs")
        except NoSuchElementException:
            pass
        return []

    _scroll_job_list(driver)
    _short_delay()

    cards = _extract_job_cards(driver)
    return cards


# ---------------------------------------------------------------------------
# Main scraper
# ---------------------------------------------------------------------------

def scrape_linkedin(known_urls: set[str] = None, extended: bool = False) -> tuple[list[JobListing], list[ExcludedJob]]:
    """Run all LinkedIn searches. Returns (accepted_listings, excluded_listings).

    extended=True: pages up to 140, stops only after 7 consecutive pages with 0 new
    vacancies (or truly no more pages). Does not affect output coloring — that is
    controlled by the caller (main.py passes recolor_existing=False for partial runs).
    """
    mode = "extended" if extended else "standard"
    max_pages = MAX_PAGES_EXTENDED if extended else MAX_PAGES
    low_yield_threshold = 7 if extended else 3
    print(f"[LinkedIn] Starting scraper ({mode} mode)...")
    _known = set(u.lower().strip() for u in (known_urls or set()))

    try:
        driver = _make_driver()
    except Exception as e:
        print(f"[LinkedIn] ERROR: Could not start Chrome driver: {e}")
        return [], []

    raw_results = []
    seen_ids = set()
    excluded = []
    listings = []

    try:
        # Verify login
        print("[LinkedIn] Checking login status...")
        if not _check_login(driver):
            print("[LinkedIn] ERROR: Not logged in. Please log into LinkedIn in the Chrome profile first.")
            print(f"[LinkedIn]   Profile: {getattr(config, 'LINKEDIN_CHROME_DATA_DIR', '')}")
            return [], []
        print("[LinkedIn] -> Logged in successfully")
        _random_delay(2, 4)

        # --- Phase 1: Collect search results ---
        for keyword in config.SEARCH_QUERIES:
            if driver is None:
                break
            for location in config.SEARCH_LOCATIONS:
                if driver is None:
                    break
                print(f"[LinkedIn] Searching: '{keyword}' in '{location}'...")

                consecutive_empty = 0
                low_yield_count = 0  # pages with 0 new results
                for page in range(max_pages):
                    start = page * RESULTS_PER_PAGE
                    url = _search_url(keyword, location, start)

                    try:
                        results = _scrape_search_page(driver, url)
                    except (WebDriverException, InvalidSessionIdException) as e:
                        print(f"[LinkedIn]   -> Error on page {page + 1}: {e}")
                        driver = _restart_driver(driver)
                        if driver is None:
                            break
                        if not _check_login(driver):
                            print("[LinkedIn]   -> Lost login after restart, stopping")
                            driver = None
                            break
                        continue

                    if not results:
                        consecutive_empty += 1
                        if consecutive_empty >= 2:
                            print(f"[LinkedIn]   -> No results on 2 consecutive pages, moving on")
                            break
                        continue
                    else:
                        consecutive_empty = 0

                    new_count = 0
                    for r in results:
                        if r["id"] not in seen_ids:
                            seen_ids.add(r["id"])
                            raw_results.append(r)
                            new_count += 1

                    print(
                        f"[LinkedIn]   -> Page {page + 1}/{max_pages}: {len(results)} results "
                        f"({new_count} new, {len(seen_ids)} unique total)"
                    )

                    # Stop when too many consecutive pages yield nothing new
                    if new_count == 0:
                        low_yield_count += 1
                        if low_yield_count >= low_yield_threshold:
                            print(f"[LinkedIn]   -> {low_yield_threshold} consecutive pages with 0 new results, moving on")
                            break
                    else:
                        low_yield_count = 0

                    if len(results) < RESULTS_PER_PAGE:
                        break

                    _random_delay(2, 4)

                # Extra delay between search combos
                _random_delay(2, 4)

        print(f"[LinkedIn] Total unique raw results: {len(raw_results)}")

        # --- Phase 2: Title filter ---
        needs_detail = []

        for r in raw_results:
            title = r["title"]
            company = r["company"]
            location = r["location"]
            url = r["url"]

            if title:
                passes, reason = check_title_filter(title)
                if not passes:
                    excluded.append(ExcludedJob("LinkedIn", title, company, location, url, reason))
                    continue
            # Empty title: defer filter to after detail fetch (title extracted from detail page)

            needs_detail.append(r)

        # Skip jobs already in spreadsheet
        if _known:
            before = len(needs_detail)
            needs_detail = [r for r in needs_detail if r["url"].lower().strip() not in _known]
            skipped = before - len(needs_detail)
            if skipped:
                print(f"[LinkedIn] Skipped {skipped} already in spreadsheet")

        print(
            f"[LinkedIn] After title filter: {len(needs_detail)} to fetch, "
            f"{len(excluded)} excluded"
        )

        # --- Phase 3: Fetch detail pages in batches ---
        if needs_detail and driver is not None:
            print(f"[LinkedIn] Fetching {len(needs_detail)} detail pages (batch size {BATCH_SIZE})...")

            # Process in batches
            for batch_start in range(0, len(needs_detail), BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, len(needs_detail))
                batch = needs_detail[batch_start:batch_end]

                if not _is_session_alive(driver):
                    driver = _restart_driver(driver)
                    if driver is None:
                        print("[LinkedIn]   -> Browser dead, stopping detail fetch")
                        break
                    if not _check_login(driver):
                        print("[LinkedIn]   -> Lost login, stopping detail fetch")
                        break

                try:
                    batch_results = _fetch_details_batch(driver, batch)
                except (WebDriverException, InvalidSessionIdException) as e:
                    print(f"[LinkedIn]   -> Batch error: {e}")
                    driver = _restart_driver(driver)
                    if driver is None:
                        break
                    if not _check_login(driver):
                        break
                    # Retry this batch
                    try:
                        batch_results = _fetch_details_batch(driver, batch)
                    except Exception:
                        batch_results = [{"url": b["url"], "title": "", "description": "", "salary": "", "date_posted": ""} for b in batch]

                # Process batch results
                for r, detail in zip(batch, batch_results):
                    title = r["title"]
                    company = r["company"]
                    location_text = r["location"]
                    url = r["url"]
                    card_date = r.get("date_posted", "")

                    description = detail.get("description", "")
                    salary = detail.get("salary", "")
                    date_posted = detail.get("date_posted", "") or card_date

                    # If card title was empty, try detail page title then re-filter
                    if not title:
                        title = detail.get("title", "") or ""
                        if title:
                            passes, reason = check_title_filter(title)
                            if not passes:
                                excluded.append(ExcludedJob("LinkedIn", title, company, location_text, url, reason))
                                continue
                        else:
                            excluded.append(ExcludedJob("LinkedIn", "(unknown)", company, location_text, url, "Failed to extract title"))
                            continue

                    # Description filter
                    if description:
                        passes, reason = check_description_filter(description)
                        if not passes:
                            excluded.append(ExcludedJob("LinkedIn", title, company, location_text, url, reason))
                            continue

                    work_type = detect_work_type(title, location_text, description)

                    listings.append(JobListing(
                        source="LinkedIn",
                        title=title,
                        company=company,
                        location=location_text,
                        salary=salary,
                        url=url,
                        description=description,
                        date_posted=date_posted,
                        work_type=work_type,
                    ))

                print(f"[LinkedIn]   Fetched {min(batch_end, len(needs_detail))}/{len(needs_detail)}...")

                # Delay between batches — give LinkedIn breathing room
                _random_delay(3, 6)

    finally:
        try:
            proc = getattr(driver, "_chrome_proc", None) if driver else None
            if driver:
                driver.quit()
            if proc:
                proc.terminate()
                proc.wait(timeout=5)
        except Exception:
            pass

    print(f"[LinkedIn] Accepted: {len(listings)}, Excluded: {len(excluded)}")
    return deduplicate(listings), excluded


def scrape_linkedin_extended(known_urls: set[str] = None) -> tuple[list[JobListing], list[ExcludedJob]]:
    """LinkedIn scraper in extended mode.

    Stops only when:
      - LinkedIn returns no results on 2 consecutive pages (truly no more pages)
      - 7 consecutive pages have 0 new vacancies (all already seen)
      - Page 140 is reached
    """
    return scrape_linkedin(known_urls=known_urls, extended=True)


if __name__ == "__main__":
    jobs, excl = scrape_linkedin()
    for j in jobs[:5]:
        print(f"  -> {j.title} @ {j.company} -- {j.work_type}")
    print(f"\nAccepted: {len(jobs)}, Excluded: {len(excl)}")
