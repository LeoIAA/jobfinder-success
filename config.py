"""
Configuration for the UK PM Job Scraper.
Edit search terms, filters, and API keys here.
"""
import os

# --- API Keys ---
# Set via environment variable or paste directly (not recommended for git)
REED_API_KEY = os.getenv("REED_API_KEY", "13ba2b96-91dc-4a5c-934e-6592859b99aa")

# --- Search Queries ---
# Each query is run separately against every source
SEARCH_QUERIES = [
    "Product Manager",
    "Product Owner",
]

# Location searches (Reed API uses locationName param)
SEARCH_LOCATIONS = [
    "UK",
    "Remote",
]

# --- Keyword Filters ---
# Listings must match at least one INCLUDE keyword (case-insensitive, checked against title)
TITLE_INCLUDE_KEYWORDS = [
    "product manager",
    "product owner",
    "senior product",
    "associate product",
]

# Listings matching any EXCLUDE keyword in the title are dropped
TITLE_EXCLUDE_KEYWORDS = [
    "director",
    "vp ",
    "vice president",
    "chief product",
    "cpo",
    "intern",
    "graduate scheme",
    "production manager",
    "production assistant",
    "property manager",
    "project manager",
    "programme manager",
    "program manager",
    "marketing manager",
    "sales manager",
    "account manager",
    "warehouse",
    "manufacturing",
    "supply chain",
]

# Description-level exclude: drop if description contains these
DESCRIPTION_EXCLUDE_KEYWORDS = [
    "10+ years",
    "15+ years",
]

# --- Output ---
OUTPUT_FILE = "pm_jobs.xlsx"
SHEET_NAME = "Listings"
EXCLUDED_SHEET_NAME = "Excluded"
LOW_SCORE_SHEET_NAME = "Low Score"

# --- Reed API ---
REED_BASE_URL = "https://www.reed.co.uk/api/1.0"
REED_RESULTS_PER_PAGE = 100  # max allowed by Reed

# --- LinkedIn ---
# Chrome data directory for LinkedIn scraper session.
# First-time setup: run `python linkedin_login.py` to log in.
LINKEDIN_CHROME_DATA_DIR = os.getenv("LINKEDIN_CHROME_DATA_DIR", os.path.expanduser("~/chrome-linkedin"))
