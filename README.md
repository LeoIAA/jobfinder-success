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
├── scraper_cvlibrary.py # CV-Library stub
├── scraper_totaljobs.py # TotalJobs stub
├── requirements.txt
└── README.md
```
