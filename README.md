# UK PM Job Scraper

Scrapes Product Manager / Product Owner job listings from UK job boards, filters for relevance, and outputs to an Excel spreadsheet that accumulates over time.

## Quick Start

```bash
# 1. Set up virtual environment
python3 -m venv venv
source venv/bin/activate   # Linux/Mac
# venv\Scripts\activate    # Windows

# 2. Install dependencies
pip install -r requirements.txt

# 3. Set your Reed API key
export REED_API_KEY="your-api-key-here"
# Get one free at: https://www.reed.co.uk/developers

# 4. Run
python main.py
```

## What It Does

1. **Searches** Reed.co.uk for "Product Manager" and "Product Owner" across UK/London/Remote
2. **Filters** by title keywords (includes relevant PM titles, excludes Project Manager, Director, etc.)
3. **Fetches** full job details (description, salary, URL) for each match
4. **Filters** descriptions to drop overly senior or fully-onsite roles
5. **Deduplicates** against itself and any existing entries in the spreadsheet
6. **Appends** new listings to `pm_jobs.xlsx`

## Output Columns

| Column | Description |
|---|---|
| Date Scraped | When the scraper ran |
| Source | Which job board (Reed, CV-Library, etc.) |
| Title | Job title |
| Company | Employer name |
| Location | Job location |
| Salary | Salary range if listed |
| Summary | 2-sentence summary (populated via Claude Code) |
| URL | Link to the full listing |

## Generating Summaries (via Claude Code)

The `Summary` column is left blank by default. When running via Claude Code:

1. Run `python main.py` to scrape and save listings
2. Ask Claude to read the spreadsheet and generate 2-sentence summaries for each listing based on the description field
3. Claude writes the summaries back to the Summary column

## Adding More Sources (Phase 2)

- `scraper_cvlibrary.py` — CV-Library (Selenium)
- `scraper_totaljobs.py` — TotalJobs (Selenium)

Each scraper returns `list[JobListing]` and is automatically merged + deduplicated by `main.py`.

## Configuration

Edit `config.py` to adjust:
- Search keywords and locations
- Title include/exclude keyword lists
- Description exclude phrases
- Output filename

## Project Structure

```
job-scraper/
├── main.py              # Entry point / orchestrator
├── config.py            # All configuration
├── models.py            # JobListing dataclass, filters, dedup
├── output.py            # Spreadsheet read/write
├── scraper_reed.py      # Reed.co.uk API scraper (MVP)
├── scraper_cvlibrary.py # CV-Library scraper (Selenium)
├── scraper_linkedin.py  # LinkedIn scraper (Selenium)
├── scraper_totaljobs.py # TotalJobs scraper (Selenium)
├── linkedin_login.py    # One-time LinkedIn login helper
├── requirements.txt
└── README.md
```

## Changelog

### v0.42

**Scoring engine tuning**
- Base score lowered 50 → 42 to reduce score inflation on generic listings
- `senior product` stretch-title penalty increased -5 → -10
- Associate title detection: added regex `\bassociate\b` + `product` check to catch comma-formatted titles like "Associate, Product Manager"
- `familiar_domains` cap reduced: 3 pts × hits (max 9) → 2 pts × hits (max 6)
- `skills` cap reduced: 2 pts × hits (max 10) → 1 pt × hits (max 7)
- Location/work type logic revamped: remote and hybrid treated equally (both = flexible); unknown/empty work type gets −3 instead of no adjustment; removed asymmetric remote-vs-hybrid bonus
- `remote` removed from `preferred_locations` (work type logic already handles it)
- Added `pm_hub_cities`: +2 score for top UK PM job hub cities as a listing quality signal, additive even for remote roles
- Bad location penalty now triggers on any non-flexible work type (was keyed on exact `remote` string)

**LinkedIn scraper**
- Fallback description extraction via DOM TreeWalker: locates literal "About the job" text node and walks up the DOM tree to find the content block — robust against LinkedIn CSS class renames
- Selenium fallback selector list expanded: added `main` and `article`
- "About the job" prefix stripping switched from `startswith()` to `find()` — catches cases where the prefix is not at position 0

**config.py**
- `.env` file support: loads key-value pairs from a local `.env` at startup using stdlib only (no `python-dotenv` dependency); shell environment variables always take precedence over `.env`

---

### v0.4
**TotalJobs scraper**
- Full Selenium-based scraper for TotalJobs using slug-based search URLs (`/jobs/product-manager`, `/jobs/product-owner`) — the query-param search returns 124k unfiltered results and was rejected
- Extracts job cards via stable `data-at` attributes instead of hashed CSS class names that change on deploys
- Detail page extraction: description, salary, date posted, work type from metadata attributes
- VPN connectivity check at startup — fails gracefully if TotalJobs is geo-blocked
- Snippet pre-filter checks exclude keywords before fetching full detail pages
- Early-stop: bails after 2 consecutive empty pages or 3 consecutive zero-new-results pages
- Cookie banner auto-dismiss; auto-restarts browser on Chrome session death
- Pagination via `?page=N` on slug URLs, up to 40 pages per slug
- Remote roles detected via `detect_work_type()` (TotalJobs doesn't support "Remote" as a location)

**main.py**
- Shorthand output file: any positional argument ending in `.xlsx` is treated as the output filename (e.g. `python main.py totaljobs 1.xlsx`)
- `--output` flag still works; positional `.xlsx` takes precedence

---

### v0.3
**Performance & scraping**
- Search locations trimmed from `["UK", "London", "Remote"]` to `["UK", "Remote"]` — London is already covered by the UK-wide search, eliminating ~33% of redundant queries
- LinkedIn `MAX_PAGES` raised from 5 → 37 → 69, with bail-out after 2 consecutive empty pages
- Batch tab loading for LinkedIn detail pages: 7 tabs opened in parallel (4+3 waves, 2–3.5s gap between waves, 1–1.5s stagger per tab); ~3× faster detail fetching
- Single JS extraction per LinkedIn detail tab replaces ~15 sequential Selenium calls (~50ms vs ~3–10s per tab)
- LinkedIn search page timing tightened: load delay 2–4s (was 3–6s), scroll iterations 4 (was 6), between-pages delay 2–4s (was 3–7s)
- Skip already-scraped URLs: existing URLs loaded from the spreadsheet before scraping; all scrapers accept a `known_urls` parameter
- Early stop on stale pages: `low_yield_count` tracker ends a search combo after 3 consecutive pages with 0 new unique job IDs
- LinkedIn 429 detection: backs off 30s and refreshes if a tab loads a "Too Many Requests" page

**Filtering**
- `check_onsite_days()` rejects listings requiring more than 1 day on site; rejected jobs go to the Excluded sheet with reason "Onsite requirement: X days"
- LinkedIn "About the job" prefix stripped from extracted descriptions

**Scoring system**
- Job scoring engine (0–100) evaluating role level, experience gap, domain fit, skills, hard requirements, location/work type preferences, contract signals, and bonus keywords
- Jobs scoring below 60 routed to a separate "Low Score" sheet (dark gold header); wiped and rewritten each run
- `python main.py --rescore` — re-scores all existing jobs in-place without scraping
- Score color coding: green (high) → yellow (mid) → red (low); white font on dark backgrounds

**Deduplication**
- Three-stage dedup pipeline: exact URL match → same-source company+title similarity ≥80% → cross-source company+description similarity ≥85%
- Duplicate URLs collected onto the kept listing's "Duplicates" column

**Spreadsheet output**
- Dynamic column mapping: `output.py` reads the header row and maps column names to positions; custom column orders and extra columns are never overwritten
- Column renames: "Initial Score" → "S1", "Work Type" → "Type"
- Yellow highlight on Company cell of newly added rows; cleared automatically on next run
- Default row height set to 30pt across all sheets
- "Duplicates" column added

**CLI**
- Per-source launching: `python main.py linkedin`, `python main.py reed cvlibrary`, etc.
- Per-scraper timing and total elapsed time in final summary
- `--rescore` flag

**Bug fixes**
- LinkedIn date parsing: `_is_valid_date_text()` rejects garbage values like "Promoted" or "Viewed"
- Salary `format_salary()` fixed double-encoded UTF-8 mojibake; now outputs clean `£45,000 – £60,000`

---

### v0.2
**LinkedIn scraper**
- Selenium-based scraper connecting via `--remote-debugging-port` (avoids profile-lock and DevToolsActivePort issues)
- Dedicated Chrome data dir at `/tmp/chrome-linkedin` (configurable in `config.py`)
- `linkedin_login.py`: one-time login helper — opens Chrome, user logs in manually, session persists for future runs
- Anti-detection: disables `navigator.webdriver`, rotates user agents, random delays (2–8s), random scroll speed/distance and mouse movements
- Security check / captcha detection — pauses up to 2 minutes for manual resolution
- Searches all `SEARCH_QUERIES × SEARCH_LOCATIONS` combos, paginating up to 5 pages each
- Extracts job cards with multiple fallback selectors; clicks "Show more" to expand descriptions
- Returns `(list[JobListing], list[ExcludedJob])` matching existing scraper interface
- Auto-restarts browser and re-checks login on Chrome session death; cleans up Chrome subprocess on exit

**main.py / models.py / output.py**
- LinkedIn scraper runs first (before Reed and CV-Library)
- `Date Posted` column added (column B, after Date Scraped); `date_posted` field now included in `to_row()` and `SPREADSHEET_COLUMNS`
- Column widths updated (A–K instead of A–J)

**config.py**
- Added `LINKEDIN_CHROME_DATA_DIR` setting (defaults to `/tmp/chrome-linkedin`)

---

### v0.1
**CV-Library scraper**
- Full Selenium-based scraper for CV-Library search and detail pages
- Extracts job data from `article.job` data attributes (title, company, location, salary, date posted)
- Fetches full descriptions from detail pages; paginates up to 5 pages per search query
- Snippet pre-filter: checks exclude keywords against search-page preview before fetching detail pages
- Auto-restarts browser on Chrome session death; cookie banner auto-dismiss

**main.py**
- CV-Library and TotalJobs now return `(listings, excluded)` tuples matching Reed's interface
- Deduplication added for the Excluded sheet (was writing duplicates across overlapping searches)

**models.py / output.py**
- `Description` column (column J) added to spreadsheet output

**scraper_totaljobs.py**
- Stub updated to return `([], [])` tuple, ready for future implementation

**scraper_cvlibrary.py — `_get_description_selenium` fixes**
- `innerHTML` fallback if `.text` returns empty (common in headless when overlays obscure the element)
- Multiple selector fallback: tries `.job__description`, `[class*='job__description']`, `.job-description`, `#job-description` in sequence
- Page load delay bumped from 1s to 1.5s
