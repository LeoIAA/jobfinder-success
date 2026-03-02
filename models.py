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
    secondary_score: Optional[int] = None   # S2: CV-fit score (multiplicative model)
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
        "artificial intelligence", "llm", "generative ai",
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
        "payment rails", "settlement model",
        "rtp", "rng", "volatility model",
        "knowledge management",
        "amazon ecosystem", "amazon seller",
        "financial planning & analysis", "fp&a", "consolidation",
        # Data/analytics PM specialism — specific enough to signal a dedicated Data PM role
        "data catalog", "data catalogue",
        "data lineage", "master data",
    ],

    "preferred_locations": ["nationwide", "united kingdom", "london"],
    "commutable_locations": ["folkestone", "kent", "south east"],
    "bad_locations": ["birmingham", "glasgow", "cardiff", "scotland"],
    # Top UK cities for PM/tech jobs — small bonus as quality signal even for remote roles
    "pm_hub_cities": [
        "manchester", "bristol", "edinburgh", "cambridge",
        "leeds", "oxford", "reading",
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
        score -= 16
    if any(x in title for x in profile["overleveled_titles"]):
        score -= 20

    # Years of experience requirement.
    # Scan title + first 1500 chars of description (reaches qualifications sections).
    # Multiple patterns to catch real-world JD formats:
    #   "5+ years of experience", "5 years experience"
    #   "5-7 years of experience"  (use lower bound — that's the minimum)
    #   "Experience: 5+ years", "experience of 5 years"
    #   "minimum 5 years", "at least 5 years"
    yr_text = title + ' ' + desc[:1500]
    yr_req = []

    # Ranges first (e.g. "5-7 years") — record lower bound, then mask to avoid double-counting
    for m in re.finditer(r'(\d+)\s*[-\u2013]\s*\d+\s*(?:years?|yrs?)', yr_text, re.IGNORECASE):
        y = int(m.group(1))
        if 1 <= y <= 20:
            yr_req.append(y)
    yr_text_masked = re.sub(r'\d+\s*[-\u2013]\s*\d+\s*(?:years?|yrs?)', 'MASKED', yr_text, flags=re.IGNORECASE)

    for pat in [
        r'(\d+)\+?\s*(?:years?|yrs?)\s*(?:of\s*)?(?:experience|exp)',       # "5+ years of experience"
        r'(?:experience|exp)[:\s]+(?:of\s+)?(\d+)\+?\s*(?:years?|yrs?)',    # "Experience: 5+ years"
        r'(?:minimum|at\s+least|min\.?)\s+(\d+)\+?\s*(?:years?|yrs?)',      # "minimum 5 years"
    ]:
        for m in re.finditer(pat, yr_text_masked, re.IGNORECASE):
            y = int(m.group(1))
            if 1 <= y <= 20:
                yr_req.append(y)

    if yr_req:
        max_yrs = max(yr_req)
        gap = max_yrs - profile["years_experience"]
        if gap <= 0:
            score += 5
        elif gap == 1:
            score -= 3
        elif gap == 2:
            score -= 7
        elif gap <= 4:
            score -= 14
        else:
            score -= 20

    # "Head of Product" in description (not in title) means the role reports to HoP —
    # confirms mid-level PM seniority, appropriate for candidate.
    if "head of product" in desc and "head of product" not in title:
        score += 4

    # --- DOMAIN FIT ---
    strong_hits = sum(1 for k in profile["strong_domains"] if k in combined)
    score += min(strong_hits * 3, 12)

    familiar_hits = sum(1 for k in profile["familiar_domains"] if k in combined)
    score += min(familiar_hits * 2, 6)

    if any(k in combined for k in profile["unfamiliar_domains"]):
        score -= 6
    if any(k in combined for k in profile["dealbreaker_domains"]):
        score -= 15

    specialism_hits = sum(1 for k in profile["deep_specialism_keywords"] if k in combined)
    if specialism_hits >= 1:
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
    is_flexible = 'remote' in work_type or 'hybrid' in work_type

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

    # --- SALARY CAP ---
    # If listed salary clearly exceeds the candidate's range it signals overleveled role
    if job.salary:
        sal_nums = re.findall(r'(\d[\d,]+)', job.salary.replace(' ', ''))
        sal_nums = [int(n.replace(',', '')) for n in sal_nums]
        if sal_nums:
            sal_max = max(sal_nums)
            if sal_max >= 110000:
                score -= 12

    # --- CONTRACT / FTC SIGNALS ---
    contract_keywords = [
        "fixed term", "fixed-term", " ftc", "(ftc)", "maternity cover",
        "secondment", "interim role", "contract role", "temporary role",
    ]
    if any(k in desc for k in contract_keywords):
        score -= 5

    # --- BONUS SIGNALS ---
    bonus_hits = sum(1 for k in profile["bonus_keywords"] if k in combined)
    score += min(bonus_hits * 2, 6)

    return max(0, min(100, score))


# ── S2: CV-fit scoring (multiplicative model) ─────────────────────────────────
# Scores every job 0-100 based on how well it fits Leo's CV profile.
# Unlike S1 (which needs ≥50 chars of description), S2 always returns a value
# so title-only / stub rows still get rated.
#
# model:  final = clamp( round(base × location_factor), 0, 100 )
#   base = role_pts + domain_pts + exp_pts + bonuses   (max ~80)
#   location_factor: Remote=1.10, London Hybrid=1.05, UK Hybrid=0.95,
#                    Edinburgh Hybrid=0.78, Manchester Onsite=0.22, …
#
# Calibrated against 439 existing S3 manual ratings (MAE ≈ 4 pts).

def _s2_clean_title(raw: str) -> str:
    """Strip LinkedIn 'with verification' multi-line artefacts."""
    return re.sub(r"\n.*", "", str(raw)).strip() if raw else ""


def _s2_role_pts(t: str) -> int:
    """0-30: role type / seniority fit."""
    if re.search(r"\b(director|vp |vice.president|head of product|cpo|chief product"
                 r"|group product manager|gm product|general manager product)\b", t):
        return 5
    if re.search(r"\b(principal product|staff product)\b", t):
        return 12
    is_senior = bool(re.search(r"\bsenior\b", t))
    is_lead   = bool(re.search(r"\b(lead product|product lead)\b", t))
    if (is_senior or is_lead) and re.search(r"\b(product (manager|owner)|pm\b|po\b)", t):
        return 20
    if re.search(r"\b(product (manager|owner|management))\b", t):
        if re.search(r"\b(junior|associate|graduate|entry.?level|intern)\b", t):
            return 11
        return 28
    if re.search(r"\bproduct owner analyst\b", t):
        return 14
    if re.search(r"\bproduct analyst\b", t):
        return 16
    if re.search(r"\b(programme manager|delivery manager|project manager)\b", t):
        return 7
    if re.search(r"\b(business analyst|ba role)\b", t):
        return 6
    if re.search(r"\b(scrum master|agile coach)\b", t):
        return 8
    return 10


def _s2_domain_pts(combined: str) -> int:
    """0-35: domain/industry relevance."""
    hard_no = [
        "medical device", "clinical trial", "nhs ", " nhs", "pharmaceutical", "pharma ",
        "defence", "defense", "military", "civil servant", "government digital",
        "embedded system", "firmware", "automotive product", "aerospace product",
        "oil and gas", "nuclear", "mining product",
    ]
    if any(w in combined for w in hard_no):
        return 5

    weak = [
        "healthcare", "health care", "medtech", "biotech", "life science",
        "social housing", "non-profit", "public sector", "local government",
        "retail banking credit", "mortgage product", "manufacturing", "construction tech",
    ]
    if any(w in combined for w in weak):
        return 11

    excellent = [
        "fintech", "payments", "payment platform", "trading platform", "broker platform",
        "forex", "crypto product", "defi", "neobank", "challenger bank", "banking platform",
        "b2b saas", "crm product", "wealth management platform", "regtech", "insurtech",
        "financial service product", "open banking", "cross-border payment", "remittance",
        "card product", "lending platform", "capital markets", "investment platform",
        "digital assets", "liquidity management",
    ]
    exc_hits = sum(1 for w in excellent if w in combined)
    if exc_hits >= 2:
        return 34
    if exc_hits == 1:
        return 29

    good = [
        "saas", "b2b", "enterprise software", "platform product",
        "automation product", "workflow automation", "internal tool",
        "data product", "analytics platform", "ai product", "ml product",
        "machine learning product", "developer tool", "api product",
        "martech", "adtech", "hr tech", "legal tech", "edtech", "proptech",
        "gaming", "game product", "digital entertainment",
        "e-commerce platform", "marketplace product",
    ]
    good_hits = sum(1 for w in good if w in combined)
    if good_hits >= 3:
        return 27
    if good_hits >= 2:
        return 25
    if good_hits == 1:
        return 18

    consumer = ["consumer product", "mobile app product", "website product",
                 "retail product", "consumer tech", "d2c"]
    if any(w in combined for w in consumer):
        return 13

    return 15


def _s2_exp_pts(t: str, d: str) -> int:
    """0-15: years-of-experience fit (Leo has ~3 yrs PM)."""
    all_years = re.findall(
        r"(\d+)\+?\s*(?:to\s*\d+\s*)?years?\s*(?:of\s*)?(?:proven\s*)?(?:product\s*)?"
        r"(?:management\s*)?experience",
        d,
    )
    if all_years:
        mn = min(int(y) for y in all_years)
        if mn <= 2:
            return 12
        if mn <= 4:
            return 14
        if mn <= 6:
            return 9
        return 3
    if re.search(r"\b(junior|associate|graduate|entry)\b", t):
        return 8
    if re.search(r"\b(senior|lead|principal)\b", t):
        return 9
    if re.search(r"\b(director|head|vp|chief)\b", t):
        return 3
    return 11


def _s2_location_factor(location: str, work_type: str, desc: str) -> float:
    """Multiplicative factor for remote/location feasibility."""
    loc = (str(location or "") + " " + str(work_type or "") + " " + (desc or "")).lower()

    # Explicit full-remote
    if re.search(r"\bfully remote\b|\bremote (only|first|based|working)\b|\(remote\)", loc):
        return 1.10

    is_hybrid = "hybrid" in loc
    is_remote = "remote" in loc
    is_onsite = bool(re.search(r"\bon.?site\b|\bin.?office\b", loc))

    if is_remote and not is_hybrid and not is_onsite:
        return 1.10

    northern_cities = (r"\b(manchester|birmingham|liverpool|sheffield|nottingham"
                       r"|newcastle|sunderland|coventry|bradford|wolverhampton)\b")
    scotland_wales  = (r"\b(glasgow|aberdeen|dundee|cardiff|belfast|swansea|edinburgh)\b")
    se_cities       = (r"\b(brighton|guildford|reading|oxford|cambridge|watford|slough"
                       r"|luton|hertford|milton keynes|southampton|portsmouth|bournemouth)\b")
    rural_counties  = (r"\b(lincolnshire|norfolk|suffolk|devon|cornwall|dorset"
                       r"|wiltshire|somerset|herefordshire|shropshire|cumbria"
                       r"|northumberland|rutland|leicestershire|derbyshire"
                       r"|staffordshire|worcestershire|gloucestershire"
                       r"|cambridgeshire|northamptonshire|buckinghamshire)\b")

    # "Hybrid/Remote" type → treat as nearly remote
    job_type_lower = str(work_type or "").lower()
    if "remote" in job_type_lower:
        return 1.08

    if is_hybrid or (is_remote and is_hybrid):
        if re.search(r"\blondon\b", loc):
            return 1.05
        if re.search(northern_cities, loc):
            return 0.40
        if re.search(r"\bedinburgh\b", loc):
            return 0.78
        if re.search(r"\bbristol\b", loc):
            return 0.82
        if re.search(scotland_wales, loc):
            return 0.50
        if re.search(se_cities, loc):
            return 0.90
        if re.search(rural_counties, loc):
            return 0.32
        if re.search(r"\b(united kingdom|uk |england|nationwide)\b", loc):
            return 0.95
        return 0.75

    if is_onsite:
        if re.search(r"\blondon\b", loc):
            return 0.45
        if re.search(northern_cities, loc):
            return 0.22
        if re.search(scotland_wales, loc):
            return 0.22
        return 0.35

    # No type specified — infer from city
    if re.search(r"\blondon\b", loc):
        return 0.95
    if re.search(northern_cities, loc):
        return 0.50
    if re.search(scotland_wales, loc):
        return 0.50
    if re.search(r"\b(united kingdom|uk |england)\b", loc):
        return 1.00
    return 0.90


def score_job_s2(job: JobListing) -> int:
    """
    CV-fit score (0-100) for Leo's profile.  Always returns an integer
    (works on title-only rows unlike S1 which needs a full description).

    Scoring model: final = clamp(round(base × location_factor), 0, 100)
    """
    raw_title = _s2_clean_title(job.title)
    desc = str(job.description).lower() if job.description else ""
    t = raw_title.lower()
    combined = t + " " + desc

    # Hard dealbreaker: language requirement Leo can't meet
    if re.search(r"\b(mandarin|cantonese|chinese.speaking|arabic.speaking"
                 r"|japanese.speaking|korean.speaking|fluent in (mandarin|arabic|japanese|korean))\b",
                 combined):
        return 5

    base = _s2_role_pts(t) + _s2_domain_pts(combined) + _s2_exp_pts(t, desc)

    # Skill-match bonus
    skill_hits = sum(1 for kw in [
        "roadmap", "backlog", "agile", "scrum", "sprint", "stakeholder",
        "cross-functional", "user story", "jtbd", "jira",
    ] if kw in combined)
    if skill_hits >= 5:
        base += 4
    elif skill_hits >= 3:
        base += 2

    # Direct background match bonus
    if any(w in combined for w in [
        "trading platform", "crm product", "broker platform",
        "payment platform", "b2b fintech", "saas crm", "financial crm",
    ]):
        base += 3

    # Contract/interim penalty
    if re.search(r"\b(contract role|interim|fixed.?term|ftc|day rate"
                 r"|12.month contract|6.month contract)\b", combined):
        base -= 6

    base = max(5, min(80, base))

    loc_factor = _s2_location_factor(job.location, job.work_type, desc)
    return max(0, min(100, round(base * loc_factor)))


def score_listings(listings: list[JobListing]) -> None:
    """Score all listings in-place, setting initial_score (S1) and secondary_score (S2)."""
    scored = 0
    for job in listings:
        job.initial_score = score_job(job)
        job.secondary_score = score_job_s2(job)
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
