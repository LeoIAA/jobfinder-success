"""
Shared data models, filtering, deduplication, and scoring logic.
"""
from dataclasses import dataclass, field
from datetime import datetime
from typing import Optional
from collections import defaultdict
from difflib import SequenceMatcher
import re
import config


@dataclass
class JobListing:
    source: str
    title: str
    company: str
    location: str
    salary: str
    url: str
    description: str
    date_posted: str
    date_scraped: str = field(default_factory=lambda: datetime.now().strftime("%Y-%m-%d"))
    summary: str = ""
    work_type: str = ""
    initial_score: Optional[int] = None
    duplicate_urls: list = field(default_factory=list)

    def to_row(self) -> list:
        return [
            self.date_scraped,
            self.date_posted,
            self.source,
            self.title,
            self.company,
            self.location,
            self.work_type,
            self.salary,
            self.initial_score if self.initial_score is not None else "",
            self.summary,
            self.url,
            self.description,
        ]


@dataclass
class ExcludedJob:
    source: str
    title: str
    company: str
    location: str
    url: str
    reason: str

    def to_row(self) -> list:
        return [self.source, self.title, self.company, self.location, self.url, self.reason]


SPREADSHEET_COLUMNS = [
    "Date Scraped",
    "Date Posted",
    "Source",
    "Title",
    "Company",
    "Location",
    "Type",
    "Salary",
    "URL",
    "Description",
    "S1",
]

EXCLUDED_COLUMNS = [
    "Source",
    "Title",
    "Company",
    "Location",
    "URL",
    "Description",
    "Exclusion Reason",
]


def check_title_filter(title: str) -> tuple[bool, str]:
    t = title.lower()
    has_include = any(kw in t for kw in config.TITLE_INCLUDE_KEYWORDS)
    if not has_include:
        return False, "Title doesn't match any include keywords"
    for kw in config.TITLE_EXCLUDE_KEYWORDS:
        if kw in t:
            return False, f"Title exclude keyword: '{kw.strip()}'"
    return True, ""


def check_description_filter(description: str) -> tuple[bool, str]:
    d = description.lower()
    for kw in config.DESCRIPTION_EXCLUDE_KEYWORDS:
        if kw in d:
            return False, f"Description exclude keyword: '{kw}'"
    return True, ""


def check_onsite_days(description: str) -> tuple[bool, str]:
    """
    Check for onsite requirements like "3 days on site", "4 days in office", etc.
    Reject if more than 1 day required on site.
    Returns (passes, reason).
    """
    d = description.lower()
    # Match patterns like "3 days on site", "4 days in office", "3 days per week in office",
    # "3 days a week on-site", "three days in the office"
    word_to_num = {
        "two": 2, "three": 3, "four": 4, "five": 5,
    }
    # Numeric patterns
    matches = re.findall(
        r'(\d)\s*(?:days?\s*(?:per\s*week\s*)?(?:on[\s-]?site|in[\s-]?(?:the\s*)?office|in[\s-]?person))',
        d,
    )
    for m in matches:
        days = int(m)
        if days > 1:
            return False, f"Onsite requirement: {days} days"

    # Word-based patterns
    for word, num in word_to_num.items():
        if re.search(
            rf'{word}\s*days?\s*(?:per\s*week\s*)?(?:on[\s-]?site|in[\s-]?(?:the\s*)?office|in[\s-]?person)',
            d,
        ):
            if num > 1:
                return False, f"Onsite requirement: {num} days"

    return True, ""


def detect_work_type(title: str, location: str, description: str) -> str:
    combined = f"{title} {location} {description}".lower()
    is_remote = any(w in combined for w in [
        "remote", "work from home", "wfh", "fully remote", "100% remote",
    ])
    is_hybrid = any(w in combined for w in [
        "hybrid", "1 day in office", "2 days in office", "flexible working",
        "1 day per week", "2 days per week", "once a month", "twice a month",
    ])
    if is_remote and is_hybrid:
        return "Hybrid/Remote"
    elif is_remote:
        return "Remote"
    elif is_hybrid:
        return "Hybrid"
    return ""


def format_salary(min_sal: Optional[float], max_sal: Optional[float]) -> str:
    if min_sal and max_sal:
        return f"\u00a3{min_sal:,.0f} \u2013 \u00a3{max_sal:,.0f}"
    elif min_sal:
        return f"From \u00a3{min_sal:,.0f}"
    elif max_sal:
        return f"Up to \u00a3{max_sal:,.0f}"
    return ""


# ============================================================
# DEDUPLICATION - 3-stage pipeline
# ============================================================

TITLE_SIMILARITY_THRESHOLD = 0.80
DESC_SIMILARITY_THRESHOLD = 0.85
DESC_COMPARE_LENGTH = 300


def _normalize_title(t: str) -> str:
    t = str(t).lower().split('\n')[0].strip()
    t = re.sub(r'\s*with verification\s*', '', t)
    t = re.sub(r'\s+', ' ', t)
    return t.strip()


def _normalize_company(c: str) -> str:
    c = str(c).lower().strip()
    for suffix in [' ltd', ' limited', ' inc', ' inc.', ' llc', ' plc',
                   ' group', ' recruitment', ' solutions', ' consulting']:
        c = c.rstrip('.')
        if c.endswith(suffix):
            c = c[:-len(suffix)]
    c = re.sub(r'[^a-z0-9 ]', '', c)
    c = re.sub(r'\s+', ' ', c)
    return c.strip()


def _normalize_url(u: str) -> str:
    u = str(u).strip().rstrip('/')
    u = re.sub(r'^https?://(www\.)?', '', u)
    return u.lower()


def _desc_snippet(d: str) -> str:
    d = str(d).lower()
    d = re.sub(r'\s+', ' ', d).strip()
    return d[:DESC_COMPARE_LENGTH]


def _similarity(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio()


def deduplicate(
    listings: list[JobListing],
    existing_urls: set[str] = None,
) -> list[JobListing]:
    """
    Three-stage deduplication pipeline:
      1. Exact URL match (including against existing spreadsheet URLs)
      2. Same-source: normalized company + similar title (>=80%)
      3. Cross-source: normalized company + description similarity (>=85%)
    Duplicates are not discarded -- their URLs are collected onto the
    kept listing's duplicate_urls field.
    Returns only unique listings.
    """
    if existing_urls is None:
        existing_urls = set()

    n = len(listings)
    if n == 0:
        return []

    is_dupe = [False] * n
    # Track which kept listing each dupe maps to
    dupe_of = [None] * n  # index of the original listing

    norm_urls = [_normalize_url(j.url) for j in listings]
    norm_titles = [_normalize_title(j.title) for j in listings]
    norm_companies = [_normalize_company(j.company) for j in listings]
    desc_snippets = [_desc_snippet(j.description) if j.description else '' for j in listings]
    sources = [j.source.lower() for j in listings]

    existing_norm = set(_normalize_url(u) for u in existing_urls)

    # --- Stage 1: Exact URL match ---
    url_first_seen = {}
    stage1 = 0
    for i in range(n):
        if norm_urls[i] in existing_norm:
            is_dupe[i] = True
            stage1 += 1
            # Already in spreadsheet, no kept listing to attach to
        elif norm_urls[i] in url_first_seen:
            is_dupe[i] = True
            dupe_of[i] = url_first_seen[norm_urls[i]]
            stage1 += 1
        else:
            url_first_seen[norm_urls[i]] = i

    # --- Stage 2: Same source, same company, similar title ---
    source_company_groups = defaultdict(list)
    for i in range(n):
        if is_dupe[i]:
            continue
        key = (sources[i], norm_companies[i])
        source_company_groups[key].append(i)

    stage2 = 0
    for key, indices in source_company_groups.items():
        if len(indices) < 2:
            continue
        for j in range(1, len(indices)):
            if is_dupe[indices[j]]:
                continue
            for k in range(j):
                if is_dupe[indices[k]]:
                    continue
                sim = _similarity(norm_titles[indices[j]], norm_titles[indices[k]])
                if sim >= TITLE_SIMILARITY_THRESHOLD:
                    is_dupe[indices[j]] = True
                    dupe_of[indices[j]] = indices[k]
                    stage2 += 1
                    break

    # --- Stage 3: Cross-source, same company, similar description ---
    company_groups = defaultdict(list)
    for i in range(n):
        if is_dupe[i]:
            continue
        company_groups[norm_companies[i]].append(i)

    stage3 = 0
    for company, indices in company_groups.items():
        if len(indices) < 2:
            continue
        for j in range(1, len(indices)):
            if is_dupe[indices[j]]:
                continue
            for k in range(j):
                if is_dupe[indices[k]]:
                    continue
                if sources[indices[j]] == sources[indices[k]]:
                    continue
                if not desc_snippets[indices[j]] or not desc_snippets[indices[k]]:
                    continue
                sim = _similarity(desc_snippets[indices[j]], desc_snippets[indices[k]])
                if sim >= DESC_SIMILARITY_THRESHOLD:
                    is_dupe[indices[j]] = True
                    dupe_of[indices[j]] = indices[k]
                    stage3 += 1
                    break

    # Collect duplicate URLs onto the kept listings (skip if same URL)
    for i in range(n):
        if is_dupe[i] and dupe_of[i] is not None:
            dupe_url = listings[i].url
            kept_url = listings[dupe_of[i]].url
            if _normalize_url(dupe_url) != _normalize_url(kept_url):
                listings[dupe_of[i]].duplicate_urls.append(dupe_url)

    total = sum(is_dupe)
    print(f"[Dedup] Stage 1 (exact URL):            {stage1}")
    print(f"[Dedup] Stage 2 (same source+company):  {stage2}")
    print(f"[Dedup] Stage 3 (cross-source desc):    {stage3}")
    print(f"[Dedup] Total removed: {total} / {n}  ->  {n - total} unique")

    return [listings[i] for i in range(n) if not is_dupe[i]]


# ============================================================
# JOB SCORING ENGINE
# ============================================================

SCORING_PROFILE = {
    "years_experience": 3,

    "target_titles": [
        "product manager", "product owner",
    ],
    "underleveled_titles": [
        "associate product", "associate, product", "junior product",
    ],
    "stretch_titles": [
        "senior product",
    ],
    "overleveled_titles": [
        "principal", "staff", "director", "head of product",
        "vp ", "chief product", "group product", "lead product",
    ],
    "too_junior_titles": [
        "graduate", "intern", "apprentice",
    ],

    "strong_domains": [
        "b2b", "saas", "crm", "internal tools", "internal platform",
        "operations platform", "fintech", "trading", "forex",
        "payments", "financial services", "financial technology",
    ],
    "familiar_domains": [
        "ai ", "artificial intelligence", "llm", "generative ai",
        "ai-powered", "ai product", "agentic",
    ],
    "unfamiliar_domains": [
        "healthcare", "pharma", "clinical", "medical device",
        "life science", "biotech",
        "embedded", "firmware", "hardware", "semiconductor",
        "automotive", "connected vehicle",
        "edtech", "curriculum", "learning design",
    ],
    "dealbreaker_domains": [
        "data engineer", "machine learning engineer", "ml engineer",
        "devops engineer", "software engineer",
    ],

    "skills": [
        "agile", "scrum", "sprint", "backlog", "user stories",
        "roadmap", "product strategy", "product vision",
        "stakeholder", "cross-functional", "cross functional",
        "ux", "user experience", "figma", "design",
        "migration", "legacy", "modernisation", "modernization",
        "transformation",
    ],
    "tools": [
        "jira", "confluence", "notion", "figma", "miro",
        "sql", "amplitude", "google analytics", "power bi",
    ],

    "lacking_requirements": [
        ("servicenow", "certif"),
        ("salesforce certified",),
        ("aws certified",),
        ("azure certified",),
        ("former engineer",),
        ("computer science degree required",),
        ("coding required",),
        ("kubernetes",),
        ("big tech",),
    ],
    "deep_specialism_keywords": [
        "etl pipeline", "data warehouse", "snowflake",
        "payment rails", "settlement model", "psp",
        "rtp", "rng", "volatility model",
        "knowledge management", "assurance",
        "amazon ecosystem", "amazon seller",
        "financial planning & analysis", "fp&a", "consolidation",
    ],

    "preferred_locations": ["nationwide", "united kingdom"],
    "commutable_locations": ["london", "folkestone", "kent", "south east"],
    "bad_locations": ["manchester", "birmingham", "leeds", "bristol",
                      "edinburgh", "glasgow", "cardiff", "scotland"],
    # Top UK cities for PM/tech jobs — small bonus as quality signal even for remote roles
    "pm_hub_cities": [
        "manchester", "bristol", "edinburgh", "cambridge",
        "birmingham", "leeds", "oxford", "reading",
        "brighton", "bath", "guildford", "newcastle",
        "sheffield", "nottingham",
    ],

    "salary_min": 50000,
    "salary_max": 85000,

    "bonus_keywords": [
        "startup", "scale-up", "scaleup", "early stage",
        "series a", "series b",
        "data-driven", "data driven", "analytics", "metrics",
        "multilingual", "russian", "international", "global",
    ],
}


def score_job(job: JobListing, profile: dict = None) -> Optional[int]:
    """Score a single job listing 0-100 based on fit to candidate profile."""
    if profile is None:
        profile = SCORING_PROFILE

    title = job.title.lower()
    desc = job.description.lower() if job.description else ""
    location = job.location.lower()
    work_type = job.work_type.lower() if job.work_type else ""

    if not desc or len(desc) < 50:
        return None

    combined = title + ' ' + desc
    score = 42

    # --- ROLE LEVEL ---
    if any(x in title for x in profile["target_titles"]):
        score += 8
    if any(x in title for x in profile["underleveled_titles"]) or (
        re.search(r'\bassociate\b', title) and 'product' in title
    ):
        score -= 5
    if any(x in title for x in profile["too_junior_titles"]):
        score -= 25
    if any(x in title for x in profile["stretch_titles"]):
        score -= 10
    if any(x in title for x in profile["overleveled_titles"]):
        score -= 18

    # Years of experience requirement
    yr_matches = re.findall(
        r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)',
        combined,
    )
    if yr_matches:
        max_yrs = max(int(y) for y in yr_matches)
        gap = max_yrs - profile["years_experience"]
        if gap <= 0:
            score += 5
        elif gap <= 2:
            pass
        elif gap <= 4:
            score -= 8
        else:
            score -= 18

    # --- DOMAIN FIT ---
    strong_hits = sum(1 for k in profile["strong_domains"] if k in combined)
    score += min(strong_hits * 4, 16)

    familiar_hits = sum(1 for k in profile["familiar_domains"] if k in combined)
    score += min(familiar_hits * 2, 6)

    if any(k in combined for k in profile["unfamiliar_domains"]):
        score -= 6
    if any(k in combined for k in profile["dealbreaker_domains"]):
        score -= 15

    specialism_hits = sum(1 for k in profile["deep_specialism_keywords"] if k in combined)
    if specialism_hits >= 2:
        score -= specialism_hits * 4

    # --- SKILLS & TOOLS ---
    skill_hits = sum(1 for k in profile["skills"] if k in combined)
    score += min(skill_hits * 1, 7)

    tool_hits = sum(1 for t in profile["tools"] if t in combined)
    score += min(tool_hits * 1, 4)

    # --- HARD REQUIREMENTS CANDIDATE LACKS ---
    for req_tuple in profile["lacking_requirements"]:
        if all(term in combined for term in req_tuple):
            score -= 10

    # --- LOCATION & WORK TYPE ---
    # Remote = Hybrid for candidate (based abroad, works remotely either way)
    # Only penalise if clearly onsite (empty work_type = likely onsite or unknown)
    is_flexible = 'remote' in work_type or 'hybrid' in work_type
    if not is_flexible and not work_type:
        score -= 3

    if any(x in location for x in profile["preferred_locations"]):
        score += 5
    elif any(x in location for x in profile["commutable_locations"]):
        score += 3
    elif any(x in location for x in profile["bad_locations"]):
        if not is_flexible:
            score -= 10

    # Bonus for top UK PM job hubs (quality signal, additive even for remote roles)
    if any(x in location for x in profile["pm_hub_cities"]):
        score += 2

    # --- BONUS SIGNALS ---
    bonus_hits = sum(1 for k in profile["bonus_keywords"] if k in combined)
    score += min(bonus_hits * 2, 6)

    return max(0, min(100, score))


def score_listings(listings: list[JobListing]) -> None:
    """Score all listings in-place, setting initial_score on each."""
    scored = 0
    for job in listings:
        job.initial_score = score_job(job)
        if job.initial_score is not None:
            scored += 1

    valid_scores = [j.initial_score for j in listings if j.initial_score is not None]
    if valid_scores:
        avg = sum(valid_scores) / len(valid_scores)
        print(f"[Scoring] Scored {scored}/{len(listings)} listings")
        print(f"[Scoring] Mean: {avg:.0f} | Min: {min(valid_scores)} | Max: {max(valid_scores)}")
        for lo, hi in [(80, 100), (60, 79), (40, 59), (20, 39), (0, 19)]:
            n = sum(1 for s in valid_scores if lo <= s <= hi)
            if n:
                print(f"[Scoring]   {lo}-{hi}: {n}")
    else:
        print("[Scoring] No listings had enough description to score")


def score_color(score: int) -> tuple[str, str]:
    """Returns (fill_color_hex, font_color_hex) for a 0-100 score."""
    if score >= 70:
        ratio = (score - 70) / 30
        r = int(100 * (1 - ratio))
        g = int(180 + 40 * ratio)
        b = int(80 * (1 - ratio))
        font_color = 'FFFFFF' if score >= 85 else '000000'
    elif score >= 40:
        ratio = (score - 40) / 30
        r = int(255 - 155 * ratio)
        g = int(200 - 20 * ratio)
        b = 50
        font_color = '000000'
    else:
        ratio = score / 40
        r = int(220 - 20 * ratio)
        g = int(60 + 140 * ratio)
        b = 50
        font_color = 'FFFFFF' if score < 25 else '000000'
    return f'{r:02X}{g:02X}{b:02X}', font_color


def clean_html(raw: str) -> str:
    return re.sub(r"<[^>]+>", " ", raw).strip()
