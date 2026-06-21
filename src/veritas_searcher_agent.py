from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
import json
import time
import re
import html
import xml.etree.ElementTree as ET
from urllib.parse import quote
from datetime import datetime, timedelta, timezone
from email.utils import parsedate_to_datetime

app = Flask(__name__)
CORS(app)

# ========== AGENT IDENTITY ==========
AGENT_NAME = "Veritas Searcher Agent"
AGENT_VERSION = "2.0"

PROVIDER_STATUS = {
    "google_news": {"status": "unknown", "last_error": None, "last_count": 0},
    "gdelt": {"status": "unknown", "last_error": None, "last_count": 0},
    "duckduckgo": {"status": "unknown", "last_error": None, "last_count": 0},
    "wikipedia": {"status": "unknown", "last_error": None, "last_count": 0},
    "usgs": {"status": "unknown", "last_error": None, "last_count": 0},
    "eonet": {"status": "unknown", "last_error": None, "last_count": 0},
    "gdacs": {"status": "unknown", "last_error": None, "last_count": 0},
}

def record_provider(name, status, count=0, error=None):
    PROVIDER_STATUS[name] = {
        "status": status,
        "last_error": str(error)[:180] if error else None,
        "last_count": count,
        "checked_at": datetime.now(timezone.utc).isoformat(),
    }

# ========== HTTP HEADERS ==========
# Wikipedia's API returns 403 to the default python-requests User-Agent,
# so we must send a descriptive UA on every outbound request.
HTTP_HEADERS = {
    "User-Agent": "VeritasFactCheck/2.0 (https://github.com/veritas; contact: veritas@example.com)",
    "Accept": "*/*",
}

# Capitalised words that are NOT places — excluded when guessing the location
# so hazard nouns (e.g. "Wildfire") don't match every event title.
LOCATION_STOPWORDS = {
    "wildfire", "wildfires", "earthquake", "earthquakes", "magnitude", "quake",
    "tremor", "aftershock", "flood", "floods", "flooding", "cyclone", "hurricane",
    "typhoon", "storm", "storms", "volcano", "volcanic", "eruption", "tsunami",
    "landslide", "drought", "shooting", "shooter", "gunman", "terror", "terrorist",
    "attack", "bombing", "explosion", "evacuation", "evacuations", "evacuated",
    "residents", "resident", "alert", "alerts", "breaking", "update", "updates",
    "active", "near", "spreading", "ordered", "killed", "dead", "reports", "report",
    "damage", "emergency", "warning", "crisis", "incident", "depth", "felt",
    "areas", "area", "people", "this", "today", "yesterday", "morning", "tonight",
    "strikes", "struck", "hit", "hits", "magnitude",
}

def location_keywords(claim):
    """Capitalised proper nouns from the claim, minus hazard/common words."""
    return [w.lower() for w in re.findall(r'\b[A-Z][a-zA-Z]{3,}\b', claim)
            if w.lower() not in LOCATION_STOPWORDS]

# ========== TRUSTED DOMAINS (For Prioritization) ==========
TRUSTED_DOMAINS = [
    # Government (.gov)
    ".gov",
    "cdc.gov",
    "nih.gov",
    "who.int",
    "fda.gov",
    "usda.gov",
    "epa.gov",
    "state.gov",
    "whitehouse.gov",
    "congress.gov",

    # Education (.edu)
    ".edu",
    "harvard.edu",
    "stanford.edu",
    "mit.edu",
    "berkeley.edu",
    "oxford.ac.uk",
    "cambridge.org",

    # Research & Science
    "nature.com",
    "science.org",
    "plos.org",
    "pubmed.ncbi.nlm.nih.gov",
    "sciencedirect.com",
    "jstor.org",

    # News & Fact-Checking
    "apnews.com",
    "reuters.com",
    "bbc.com",
    "npr.org",
    "factcheck.org",
    "snopes.com",
    "politifact.com",
]

SEARCH_TERM_CORRECTIONS = {
    r"\btsunizia\b": "Tunisia",
    r"\btunizia\b": "Tunisia",
    r"\bworldcup\b": "World Cup",
}

SEARCH_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for",
    "was", "were", "is", "are", "there", "that", "this", "with", "from",
    "by", "as", "it", "its", "has", "have", "had", "reports", "report",
    "immediate", "some", "parts", "country", "pushed", "toward",
}

HAZARD_GROUPS = {
    "earthquake": {"earthquake", "quake", "aftershock", "seismic", "tremor", "magnitude"},
    "wildfire": {"wildfire", "wildfires", "bushfire", "forest", "brushfire"},
    "flood_storm": {"flood", "flooding", "storm", "hurricane", "typhoon", "cyclone", "tornado", "tsunami"},
    "volcano": {"volcano", "volcanic", "eruption", "lava", "ash"},
    "heat": {"heatwave", "heat", "temperature", "temperatures", "drought"},
    "landslide": {"landslide", "mudslide", "avalanche"},
}

def normalize_search_claim(claim):
    """Correct common entity typos and make sports-result queries direction-neutral."""
    normalized = re.sub(r"\s+", " ", (claim or "")).strip()
    for pattern, replacement in SEARCH_TERM_CORRECTIONS.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)

    match = re.match(
        r"^\s*(.+?)\s+(?:won|lost|beat|defeated)\s+(?:against|to)?\s*(.+?)"
        r"(?:\s+(?:for|in|at)\s+(?:the\s+)?world cup)?\s*$",
        normalized,
        flags=re.IGNORECASE,
    )
    if match:
        first = match.group(1).strip(" ,.")
        second = match.group(2).strip(" ,.")
        normalized = f"{first} {second} World Cup latest result"
    return normalized

def claim_tokens(text):
    return {
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) >= 3 and token not in SEARCH_STOPWORDS
    }

def claim_hazard_group(text, incident_type=None):
    itype = (incident_type or "").lower()
    if itype in HAZARD_GROUPS:
        return itype
    lowered = (text or "").lower()
    for group, words in HAZARD_GROUPS.items():
        if any(re.search(rf"(?<!\w){re.escape(word)}(?!\w)", lowered) for word in words):
            return group
    if itype == "other_disaster":
        return "other_disaster"
    return None

def explicit_historical_date(text):
    years = [int(y) for y in re.findall(r"\b(19\d{2}|20\d{2})\b", text or "")]
    current_year = datetime.now(timezone.utc).year
    return any(year < current_year for year in years)

def compact_search_query(claim, incident_type=None):
    normalized = normalize_search_claim(claim)
    tokens = re.findall(r"[A-Za-z0-9À-ÖØ-öø-ÿ.'’-]+", normalized)
    keep = [token for token in tokens if token.lower() not in SEARCH_STOPWORDS]
    hazard = claim_hazard_group(normalized, incident_type)
    if hazard and hazard != "other_disaster":
        canonical = {
            "earthquake": "earthquake",
            "wildfire": "wildfire",
            "flood_storm": "flood storm",
            "volcano": "volcano eruption",
            "heat": "heat wave",
            "landslide": "landslide",
        }[hazard]
        query = " ".join(keep[:9])
        if canonical.split()[0] not in query.lower():
            query = f"{query} {canonical}"
        return re.sub(r"\s+", " ", query).strip()
    return " ".join(keep[:10]) or normalized

def build_search_queries(claim, incident_type=None, location=None):
    normalized = normalize_search_claim(claim)
    compact = compact_search_query(normalized, incident_type)
    # Fold the user-supplied location into the query when it isn't already in
    # the claim, so news/GDELT search for the right place (Phase 1: context).
    loc = (location or "").strip()
    if loc and loc.lower() not in normalized.lower():
        compact = f"{compact} {loc}".strip()
    queries = [normalized]
    if compact.lower() != normalized.lower():
        queries.append(compact)
    hazard = claim_hazard_group(normalized, incident_type)
    if hazard and not explicit_historical_date(normalized):
        queries.append(f"{compact} when:30d")
    deduped = []
    seen = set()
    for query in queries:
        key = query.lower()
        if key not in seen:
            seen.add(key)
            deduped.append(query)
    return deduped[:3]

def parse_source_datetime(value):
    if not value:
        return None
    try:
        if re.fullmatch(r"\d{8}T\d{6}Z", value):
            return datetime.strptime(value, "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
        parsed = parsedate_to_datetime(value)
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return parsed.astimezone(timezone.utc)
    except Exception:
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return parsed.astimezone(timezone.utc)
        except Exception:
            return None

def freshness_score(result, time_sensitive=True):
    published = parse_source_datetime(result.get("published") or result.get("published_at"))
    if not published:
        return 0.72 if result.get("source_type") == "government" else 0.28
    age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    if not time_sensitive:
        return 0.75
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.9
    if age_hours <= 24 * 7:
        return 0.78
    if age_hours <= 24 * 30:
        return 0.58
    if age_hours <= 24 * 180:
        return 0.32
    return 0.12

def result_relevance(result, claim, incident_type=None):
    text = " ".join((
        result.get("title", "") or "",
        result.get("snippet", "") or "",
        result.get("publisher", "") or "",
    )).lower()
    tokens = claim_tokens(normalize_search_claim(claim))
    text_tokens = set(re.findall(r"[a-z0-9]+", text))
    overlap = len(tokens & text_tokens) / max(1, min(len(tokens), 10))

    hazard = claim_hazard_group(claim, incident_type)
    hazard_bonus = 0.0
    if hazard in HAZARD_GROUPS:
        hazard_bonus = 0.22 if any(word in text_tokens for word in HAZARD_GROUPS[hazard]) else -0.22

    location_words = location_keywords(claim)
    location_bonus = 0.0
    if location_words:
        matched = sum(1 for word in location_words if word in text)
        location_bonus = min(0.30, matched / len(location_words) * 0.30)
        if matched == 0:
            location_bonus = -0.20

    claim_numbers = set(re.findall(r"\b\d+(?:\.\d+)?\b", claim))
    number_bonus = 0.0
    if claim_numbers and any(number in text for number in claim_numbers):
        number_bonus = 0.15
    return max(0.0, min(1.0, overlap + hazard_bonus + location_bonus + number_bonus))

TRUST_SCORE = {
    "government": 1.0, "education": 0.95, "fact_check": 0.9,
    "research": 0.86, "news_reputable": 0.84, "encyclopedia": 0.68,
    "news": 0.62, "general": 0.42, "unknown": 0.30,
}

def rank_results(results, claim, incident_type=None):
    time_sensitive = not explicit_historical_date(claim)
    hazard = claim_hazard_group(claim, incident_type)
    ranked = []
    for result in results:
        relevance = result_relevance(result, claim, incident_type)
        freshness = freshness_score(result, time_sensitive=time_sensitive)
        trust = TRUST_SCORE.get(result.get("source_type", "unknown"), 0.3)
        result["relevance_score"] = round(relevance, 3)
        result["freshness_score"] = round(freshness, 3)
        result["trust_score"] = round(trust * 100)
        # Dynamic disaster claims demand both topical match and recency.
        if hazard and result.get("source_type") not in {"government"} and relevance < 0.18:
            continue
        if hazard and result.get("source_type") == "encyclopedia" and relevance < 0.55:
            continue
        result["rank_score"] = round(
            trust * 0.38 + relevance * 0.37 + freshness * 0.25, 4
        )
        ranked.append(result)
    return sorted(
        ranked,
        key=lambda row: (
            row.get("rank_score", 0),
            parse_source_datetime(row.get("published") or row.get("published_at"))
            or datetime.min.replace(tzinfo=timezone.utc),
        ),
        reverse=True,
    )

# ========== SEARCH FUNCTION (routes authoritative feeds by incident type) ==========
def search_web(claim, search_engine='duckduckgo', incident_type=None, location=None,
               language=None, location_en=None):
    """Search multiple sources, routing authoritative hazard feeds by the
    incident type (and claim keywords), always backed by the latest news.
    location/language (Phase 1 context) steer the news query + locale.
    location_en is the English place name used to match English data feeds
    (USGS/GDACS/EONET) when the claim itself is not in English."""
    # English geo hint so hazard-feed location matching works for non-English claims
    geo_hint = (location_en or location or "")
    queries = build_search_queries(claim, incident_type, location)
    search_claim = queries[0]
    if search_claim != claim or len(queries) > 1:
        print(f"✏️ Search queries: {queries}")
    all_results = []
    itype = (incident_type or "").lower()
    cl = search_claim.lower()
    detected_hazard = claim_hazard_group(search_claim, incident_type)
    def has(words):
        return any(w in cl for w in words)

    want_eq = itype == "earthquake" or has(["earthquake", "quake", "seismic", "magnitude", "tremor", "aftershock"])
    want_fire = itype == "wildfire" or has(["wildfire", "bushfire", "forest fire", "brush fire"])
    want_volcano = itype == "volcano" or has(["volcano", "eruption", "volcanic", "lava", "ash cloud"])
    want_storm = itype == "flood_storm" or has(["flood", "cyclone", "hurricane", "typhoon", "storm surge", "tsunami", "landslide", "drought"])
    # EONET/GDACS do not provide a useful heat-wave feed. Querying every
    # disaster type for a heat claim injects unrelated floods from the same
    # country, so route these feeds only when their event categories apply.
    want_disaster_feed = (
        want_fire or want_volcano or want_storm
        or (itype == "other_disaster" and detected_hazard not in {"heat"})
    )

    # 1) Authoritative hazard feeds, routed by incident type / keywords (no key)
    if want_eq:
        usgs = search_usgs_earthquakes(claim, geo_hint)
        if usgs:
            print(f"🌎 USGS found {len(usgs)} matching events")
        all_results.extend(usgs)
    if want_disaster_feed:
        feed_type = detected_hazard if detected_hazard in EONET_CATEGORIES else itype
        eonet = search_eonet(claim, feed_type, geo_hint)
        if eonet:
            print(f"🛰️ NASA EONET found {len(eonet)} events")
        all_results.extend(eonet)
        gdacs = search_gdacs(claim, itype, geo_hint)
        if gdacs:
            print(f"🚨 GDACS found {len(gdacs)} alerts")
        all_results.extend(gdacs)

    # 2) Latest news via multiple query shapes. The normalized full claim
    # preserves detail; the compact/time-bounded query improves recall.
    news = []
    for query in queries:
        news.extend(search_google_news(query, language))
    news = remove_duplicates(news)
    print(f"📰 Google News found {len(news)} unique results")
    all_results.extend(news)

    # 2b) GDELT — real article URLs (best-effort; skipped on rate-limit)
    gdelt = search_gdelt(compact_search_query(search_claim, incident_type))
    if gdelt:
        print(f"🌐 GDELT found {len(gdelt)} results")
    all_results.extend(gdelt)

    # 3) DuckDuckGo instant answers (encyclopedic only)
    if search_engine in ['duckduckgo', 'all']:
        ddg = search_duckduckgo(search_claim)
        all_results.extend(ddg)
        print(f"🔍 DuckDuckGo found {len(ddg)} total results")

    # 4) Wikipedia for general / historical knowledge
    wiki = search_wikipedia(search_claim)
    all_results.extend(wiki)
    print(f"📚 Wikipedia found {len(wiki)} results")

    # Remove duplicates and score by trust + topical relevance + freshness.
    unique_results = remove_duplicates(all_results)
    sorted_results = rank_results(unique_results, claim, incident_type)

    # Return a balanced mix so the LATEST NEWS is never crowded out by
    # authoritative + encyclopedic sources filling every slot.
    return diversify_results(sorted_results, limit=14)

# ========== BALANCED SELECTION ==========
def diversify_results(sorted_results, limit=14):
    """Keep a mix of authoritative, news, and encyclopedic sources."""
    NEWS_TYPES = {"news", "news_reputable", "fact_check"}
    GOV_TYPES = {"government", "education", "research"}

    gov = [r for r in sorted_results if r.get("source_type") in GOV_TYPES]
    news = [r for r in sorted_results if r.get("source_type") in NEWS_TYPES]
    enc = [r for r in sorted_results if r.get("source_type") == "encyclopedia"]
    other = [r for r in sorted_results
             if r.get("source_type") not in GOV_TYPES | NEWS_TYPES | {"encyclopedia"}]

    picked, seen = [], set()
    # quotas: authoritative ground-truth, lots of latest news, then background.
    # "other" often holds news from outlets whose name we couldn't map to a
    # domain, so it gets a generous quota too.
    for bucket, quota in ((gov, 5), (news, 10), (enc, 1), (other, 4)):
        for r in bucket[:quota]:
            if r["url"] not in seen:
                picked.append(r); seen.add(r["url"])

    # fill any remaining slots from the trust-sorted list
    for r in sorted_results:
        if len(picked) >= limit:
            break
        if r["url"] not in seen:
            picked.append(r); seen.add(r["url"])

    return picked[:limit]

# ========== DUCKDUCKGO SEARCH ==========
def search_duckduckgo(query):
    """Search DuckDuckGo and extract results"""
    try:
        print(f"🔍 Searching DuckDuckGo for: {query}")

        url = "https://api.duckduckgo.com/"
        params = {
            'q': query,
            'format': 'json',
            'no_html': 1,
            'skip_disambig': 1
        }

        response = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = []

        # Get the main abstract
        if data.get('Abstract'):
            results.append({
                "title": data.get('Heading', 'Abstract'),
                "url": data.get('AbstractURL', ''),
                "snippet": data.get('Abstract', 'No snippet available'),
                "source_type": get_source_type(data.get('AbstractURL', ''))
            })

        # Get related topics
        for topic in data.get('RelatedTopics', [])[:6]:
            if 'Text' in topic:
                results.append({
                    "title": topic.get('Text', 'Related Topic')[:50],
                    "url": topic.get('FirstURL', ''),
                    "snippet": topic.get('Text', 'No snippet available'),
                    "source_type": get_source_type(topic.get('FirstURL', ''))
                })

        record_provider("duckduckgo", "online", len(results))
        return results

    except Exception as e:
        record_provider("duckduckgo", "degraded", 0, e)
        print(f"❌ DuckDuckGo search error: {e}")
        return []

# ========== WIKIPEDIA SEARCH ==========
def search_wikipedia(claim):
    """Search Wikipedia for relevant articles"""
    try:
        print(f"🔍 Searching Wikipedia for: {claim}")

        url = "https://en.wikipedia.org/w/api.php"
        params = {
            'action': 'query',
            'list': 'search',
            'srsearch': claim,
            'format': 'json',
            'srlimit': 3
        }

        response = requests.get(url, params=params, headers=HTTP_HEADERS, timeout=10)
        response.raise_for_status()
        data = response.json()

        results = []
        for item in data.get('query', {}).get('search', []):
            title = item.get('title', 'Untitled')
            wiki_url = f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}"
            results.append({
                "title": title,
                "url": wiki_url,
                "snippet": item.get('snippet', 'No snippet available').replace('<span class="searchmatch">', '').replace('</span>', ''),
                "source_type": "encyclopedia"
            })

        record_provider("wikipedia", "online", len(results))
        return results

    except Exception as e:
        record_provider("wikipedia", "degraded", 0, e)
        print(f"❌ Wikipedia search error: {e}")
        return []

# ========== GOOGLE NEWS RSS (latest articles, no API key) ==========
# Map a coarse language hint to Google News locale params so non-US/English
# incidents return local-language coverage (Phase 1 / Phase 4 multilingual).
GOOGLE_NEWS_LOCALES = {
    "ja": ("ja", "JP", "JP:ja"), "es": ("es-419", "MX", "MX:es-419"),
    "fr": ("fr", "FR", "FR:fr"), "de": ("de", "DE", "DE:de"),
    "it": ("it", "IT", "IT:it"), "pt": ("pt-BR", "BR", "BR:pt-419"),
    "tl": ("en-PH", "PH", "PH:en"), "fil": ("en-PH", "PH", "PH:en"),
    "hi": ("hi", "IN", "IN:hi"), "ar": ("ar", "EG", "EG:ar"),
    "tr": ("tr", "TR", "TR:tr"), "el": ("el", "GR", "GR:el"),
    "ko": ("ko", "KR", "KR:ko"), "zh": ("zh-CN", "CN", "CN:zh-Hans"),
}

def search_google_news(claim, language=None):
    """Fetch the latest news articles for a claim via Google News RSS."""
    try:
        print(f"📰 Searching Google News for: {claim}")
        query = quote(claim)
        hl, gl, ceid = GOOGLE_NEWS_LOCALES.get((language or "").lower(), ("en-US", "US", "US:en"))
        url = f"https://news.google.com/rss/search?q={query}&hl={hl}&gl={gl}&ceid={ceid}"
        response = requests.get(url, headers=HTTP_HEADERS, timeout=10)
        if response.status_code != 200:
            record_provider("google_news", "degraded", 0, f"HTTP {response.status_code}")
            print(f"❌ Google News returned status {response.status_code}")
            return []

        root = ET.fromstring(response.content)
        results = []
        for item in root.findall('.//item')[:14]:
            title = item.findtext('title', 'Untitled')
            link = item.findtext('link', '')
            pub = item.findtext('pubDate', '')
            source_el = item.find('source')
            publisher = source_el.text if source_el is not None and source_el.text else ''
            desc = item.findtext('description', '') or ''
            snippet = html.unescape(re.sub(r'<[^>]+>', '', desc))
            snippet = re.sub(r'\s+', ' ', snippet).strip()[:300] or title
            results.append({
                "title": title,
                "url": link,
                "snippet": (f"[{publisher}] " if publisher else "") + snippet,
                "published": pub,
                "publisher": publisher,
                "source_type": classify_news_publisher(publisher),
                "provider": "Google News",
            })
        record_provider("google_news", "online", len(results))
        return results

    except Exception as e:
        record_provider("google_news", "degraded", 0, e)
        print(f"❌ Google News search error: {e}")
        return []

# ========== GDELT DOC 2.0 (real article URLs, no API key) ==========
# GDELT returns REAL article URLs (unlike Google News redirect links), so the
# Extractor can pull actual body text from them. Rate-limited to ~1 req / 5s,
# so this is best-effort: any error/429 is skipped silently.
GDELT_STOPWORDS = {
    "a", "an", "the", "and", "or", "of", "in", "on", "at", "to", "for", "was",
    "were", "is", "are", "there", "no", "immediate", "reports", "that", "this",
    "with", "from", "by", "as", "it", "its", "has", "have", "had", "struck",
}

def _gdelt_query(claim):
    words = re.findall(r"[A-Za-z0-9]+", claim)
    keep = [w for w in words if w.lower() not in GDELT_STOPWORDS]
    return " ".join((keep or words)[:8])

def search_gdelt(claim):
    """Search GDELT DOC 2.0 for recent news articles with real URLs."""
    try:
        query = _gdelt_query(claim)
        if not query:
            return []
        print(f"🌐 Searching GDELT for: {query}")
        params = {
            "query": query, "mode": "ArtList", "format": "json",
            "maxrecords": "12", "sort": "DateDesc", "timespan": "30d",
        }
        r = requests.get("https://api.gdeltproject.org/api/v2/doc/doc",
                         params=params, headers=HTTP_HEADERS, timeout=6)
        if r.status_code != 200 or not r.text.strip().startswith("{"):
            record_provider("gdelt", "degraded", 0, f"HTTP {r.status_code}")
            print(f"⚠️ GDELT unavailable (status {r.status_code}); skipping")
            return []
        articles = r.json().get("articles", []) or []
        results = []
        for a in articles:
            url = a.get("url", "")
            if not url:
                continue
            results.append({
                "title": a.get("title", "Untitled") or "Untitled",
                "url": url,
                "snippet": f"[{a.get('domain','')}] {a.get('title','')}".strip(),
                "published": a.get("seendate", ""),
                "source_type": get_source_type(url) if get_source_type(url) != "general" else "news",
                "provider": "GDELT",
            })
        record_provider("gdelt", "online", len(results))
        return results
    except Exception as e:
        record_provider("gdelt", "degraded", 0, e)
        print(f"⚠️ GDELT search error: {e}; skipping")
        return []

# Reputable outlets get a higher trust tier even though Google News gives us
# the publisher as a name (e.g. "Reuters") rather than a domain.
REPUTABLE_OUTLETS = [
    "reuters", "associated press", "ap news", "bbc", "npr", "the guardian",
    "new york times", "washington post", "al jazeera", "cnn", "abc news",
    "cbs", "nbc", "bloomberg", "financial times", "the times", "pbs",
    "deutsche welle", "france 24", "the independent", "sky news",
]

def classify_news_publisher(publisher):
    """Classify a Google News publisher name into a trust tier."""
    if not publisher:
        return "news"
    p = publisher.lower()
    if any(outlet in p for outlet in REPUTABLE_OUTLETS):
        return "news_reputable"
    return "news"

# ========== USGS EARTHQUAKE CATALOG (authoritative ground truth, no key) ==========
def search_usgs_earthquakes(claim, location_hint=None):
    """Query USGS for real earthquake events when a claim is seismic."""
    claim_lower = claim.lower()
    seismic = ["earthquake", "quake", "magnitude", "seismic", "richter", "tremor", "aftershock"]
    if not any(k in claim_lower for k in seismic):
        return []

    try:
        print(f"🌎 Querying USGS earthquake catalog for: {claim}")
        params = {
            "format": "geojson",
            "starttime": (datetime.now(timezone.utc) - timedelta(days=30)).strftime("%Y-%m-%d"),
            "orderby": "time",
            "limit": 50,
        }
        mag_match = re.search(r'(?:magnitude|mag|m)\s*[- ]?\s*(\d(?:\.\d+)?)', claim_lower)
        if mag_match:
            mag = float(mag_match.group(1))
            params["minmagnitude"] = max(0.0, mag - 0.7)
            params["maxmagnitude"] = mag + 0.7
        else:
            params["minmagnitude"] = 5.0

        r = requests.get("https://earthquake.usgs.gov/fdsnws/event/1/query",
                         params=params, headers=HTTP_HEADERS, timeout=12)
        if r.status_code != 200:
            record_provider("usgs", "degraded", 0, f"HTTP {r.status_code}")
            print(f"❌ USGS returned status {r.status_code}")
            return []

        features = r.json().get("features", [])
        # location keywords from the claim (capitalised proper nouns)
        loc_words = location_keywords(f"{claim} {location_hint or ''}")

        def to_source(f):
            p = f.get("properties", {})
            place = p.get("place", "") or "unknown location"
            mag = p.get("mag")
            t = p.get("time")
            when = datetime.fromtimestamp(t / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M UTC") if t else "unknown time"
            return {
                "title": f"USGS: M{mag} earthquake — {place}",
                "url": p.get("url", "https://earthquake.usgs.gov/earthquakes/"),
                "snippet": f"USGS officially recorded a magnitude {mag} earthquake at {place} on {when}.",
                "source_type": "government",
                "published": datetime.fromtimestamp(t / 1000, timezone.utc).isoformat() if t else "",
                "provider": "USGS",
                "_place": place.lower()
            }

        sources = [to_source(f) for f in features]
        if loc_words:
            # a location was named — only return events that actually match it,
            # otherwise stay silent rather than injecting unrelated quakes
            chosen = [s for s in sources if all(w in s["_place"] for w in loc_words)]
        else:
            # no location named — surface the most significant recent events
            chosen = sources[:5]
        for s in chosen:
            s.pop("_place", None)
        chosen = chosen[:8]
        record_provider("usgs", "online", len(chosen))
        return chosen

    except Exception as e:
        record_provider("usgs", "degraded", 0, e)
        print(f"❌ USGS search error: {e}")
        return []

# ========== NASA EONET (wildfire / volcano / storm — authoritative, no key) ==========
EONET_CATEGORIES = {
    "wildfire": ["wildfires"],
    "volcano": ["volcanoes"],
    "flood_storm": ["severeStorms", "floods"],
    "other_disaster": [],  # any category
}

def search_eonet(claim, incident_type=None, location_hint=None):
    """Query NASA EONET for active natural events (wildfires, volcanoes, storms, floods)."""
    try:
        print(f"🛰️ Searching NASA EONET for: {claim}")
        r = requests.get("https://eonet.gsfc.nasa.gov/api/v3/events",
                         params={"status": "open", "days": "120", "limit": "100"},
                         headers=HTTP_HEADERS, timeout=8)
        if r.status_code != 200:
            record_provider("eonet", "degraded", 0, f"HTTP {r.status_code}")
            print(f"⚠️ EONET status {r.status_code}; skipping")
            return []
        events = r.json().get("events", []) or []
        loc_words = location_keywords(f"{claim} {location_hint or ''}")
        cats = EONET_CATEGORIES.get((incident_type or "").lower())
        results = []
        for e in events:
            title = e.get("title", "") or ""
            ecats = [c.get("id") for c in e.get("categories", [])]
            if cats and not any(c in cats for c in ecats):
                continue
            if loc_words and not any(w in title.lower() for w in loc_words):
                continue
            src = (e.get("sources") or [{}])[0].get("url") or e.get("link") or "https://eonet.gsfc.nasa.gov/"
            results.append({
                "title": f"NASA EONET: {title}",
                "url": src,
                "snippet": f"NASA EONET is tracking an active natural event: {title} (category: {', '.join(ecats) or 'natural'}).",
                "source_type": "government",
                "published": ((e.get("geometry") or [{}])[-1].get("date") or ""),
                "provider": "NASA EONET",
            })
        results = results[:8]
        record_provider("eonet", "online", len(results))
        return results
    except Exception as e:
        record_provider("eonet", "degraded", 0, e)
        print(f"⚠️ EONET error: {e}; skipping")
        return []

# ========== GDACS (global disaster alerts — authoritative, no key) ==========
def search_gdacs(claim, incident_type=None, location_hint=None):
    """Query the GDACS global disaster alert feed (earthquake/flood/cyclone/volcano)."""
    try:
        print(f"🚨 Searching GDACS for: {claim}")
        r = requests.get("https://www.gdacs.org/xml/rss.xml", headers=HTTP_HEADERS, timeout=8)
        if r.status_code != 200:
            record_provider("gdacs", "degraded", 0, f"HTTP {r.status_code}")
            print(f"⚠️ GDACS status {r.status_code}; skipping")
            return []
        root = ET.fromstring(r.content)
        loc_words = location_keywords(f"{claim} {location_hint or ''}")
        results = []
        for item in root.findall('.//item'):
            title = item.findtext('title', '') or ''
            link = item.findtext('link', '') or 'https://www.gdacs.org/'
            desc = item.findtext('description', '') or ''
            blob = (title + ' ' + desc).lower()
            if loc_words and not any(w in blob for w in loc_words):
                continue
            snippet = re.sub(r'<[^>]+>', '', html.unescape(desc))
            snippet = re.sub(r'\s+', ' ', snippet).strip()[:300] or title
            results.append({
                "title": f"GDACS: {title[:100]}",
                "url": link,
                "snippet": snippet,
                "source_type": "government",
                "published": item.findtext("pubDate", "") or "",
                "provider": "GDACS",
            })
        results = results[:8]
        record_provider("gdacs", "online", len(results))
        return results
    except Exception as e:
        record_provider("gdacs", "degraded", 0, e)
        print(f"⚠️ GDACS error: {e}; skipping")
        return []

# ========== SOURCE TYPE DETECTION ==========
def get_source_type(url):
    """Determine if a URL is from a trustworthy source"""
    if not url:
        return "unknown"

    url_lower = url.lower()

    # Government sites (HIGHEST PRIORITY)
    if ".gov" in url_lower or "who.int" in url_lower or "nato.int" in url_lower:
        return "government"

    # Education sites (HIGH PRIORITY)
    if ".edu" in url_lower or "ac.uk" in url_lower or "edu.au" in url_lower:
        return "education"

    # Research & Science
    if "nature.com" in url_lower or "science.org" in url_lower or "plos.org" in url_lower:
        return "research"
    if "pubmed" in url_lower or "ncbi" in url_lower:
        return "research"

    # Fact-checking sites
    if "factcheck.org" in url_lower or "politifact.com" in url_lower or "snopes.com" in url_lower:
        return "fact_check"

    # Reputable News
    if "apnews.com" in url_lower or "reuters.com" in url_lower or "bbc.com" in url_lower:
        return "news_reputable"
    if "npr.org" in url_lower or "nytimes.com" in url_lower or "washingtonpost.com" in url_lower:
        return "news_reputable"

    # Wikipedia
    if "wikipedia.org" in url_lower:
        return "encyclopedia"

    # General News
    if "news" in url_lower or "times" in url_lower or "post" in url_lower:
        return "news"

    return "general"

# ========== REMOVE DUPLICATES ==========
def remove_duplicates(results):
    """Remove duplicate URLs from results"""
    seen_urls = set()
    unique_results = []

    for result in results:
        url = result.get('url', '')
        if url and url not in seen_urls:
            seen_urls.add(url)
            unique_results.append(result)

    return unique_results

# ========== SORT BY TRUSTWORTHINESS ==========
def sort_by_trustworthiness(results):
    """Sort results with .edu and .gov first, then others"""
    trust_score = {
        "government": 100,
        "education": 95,
        "fact_check": 90,
        "research": 85,
        "news_reputable": 80,
        "encyclopedia": 75,
        "news": 60,
        "general": 40,
        "unknown": 30,
    }

    # Add trust score to each result
    for result in results:
        source_type = result.get('source_type', 'unknown')
        result['trust_score'] = trust_score.get(source_type, 30)

    # Sort by trust score (highest first)
    sorted_results = sorted(results, key=lambda x: x.get('trust_score', 0), reverse=True)
    return sorted_results

# ========== MOCK SEARCH RESULTS (Fallback) ==========
def mock_search_results(claim):
    """Smart mock results with REAL working URLs"""
    claim_lower = claim.lower()

    if "vaccine" in claim_lower and "autism" in claim_lower:
        return [
            {"title": "Vaccines and Autism - Wikipedia", "url": "https://en.wikipedia.org/wiki/Vaccines_and_autism", "snippet": "Multiple studies show no link between vaccines and autism.", "source_type": "encyclopedia"},
            {"title": "CDC - Vaccines and Autism", "url": "https://www.cdc.gov/vaccinesafety/concerns/autism.html", "snippet": "CDC information on vaccines and autism.", "source_type": "government"}
        ]
    elif "earth" in claim_lower and "sun" in claim_lower:
        return [
            {"title": "Earth's Orbit - Wikipedia", "url": "https://en.wikipedia.org/wiki/Earth%27s_orbit", "snippet": "The Earth orbits the Sun at 67,000 mph.", "source_type": "encyclopedia"},
            {"title": "NASA - Earth's Orbit", "url": "https://www.nasa.gov/audience/forstudents/5-8/features/nasa-knows/what-is-orbit-58.html", "snippet": "NASA explains Earth's orbit around the Sun.", "source_type": "government"}
        ]
    elif "climate" in claim_lower and "change" in claim_lower:
        return [
            {"title": "Climate Change - Wikipedia", "url": "https://en.wikipedia.org/wiki/Climate_change", "snippet": "Scientific consensus confirms climate change.", "source_type": "encyclopedia"},
            {"title": "NASA Climate Change Evidence", "url": "https://climate.nasa.gov/evidence/", "snippet": "NASA's evidence for climate change.", "source_type": "government"}
        ]
    elif "hackathon" in claim_lower:
        return [
            {"title": "Hackathon - Wikipedia", "url": "https://en.wikipedia.org/wiki/Hackathon", "snippet": "A hackathon is an event where people engage in collaborative computer programming.", "source_type": "encyclopedia"},
            {"title": "Major League Hacking", "url": "https://mlh.io/", "snippet": "Official resource for hackathon participants.", "source_type": "general"}
        ]
    elif "photosynthesis" in claim_lower:
        return [
            {"title": "Photosynthesis - Wikipedia", "url": "https://en.wikipedia.org/wiki/Photosynthesis", "snippet": "The process by which plants convert sunlight into energy.", "source_type": "encyclopedia"},
            {"title": "NASA - Photosynthesis", "url": "https://www.nasa.gov/feature/goddard/2022/photosynthesis", "snippet": "NASA's research on photosynthesis.", "source_type": "government"}
        ]
    else:
        # For unknown claims, return Wikipedia search and Google Scholar
        return [
            {"title": f"Wikipedia: {claim}", "url": f"https://en.wikipedia.org/wiki/{claim.replace(' ', '_')}", "snippet": f"Wikipedia article about {claim}", "source_type": "encyclopedia"},
            {"title": f"Google Scholar search for: {claim}", "url": f"https://scholar.google.com/scholar?q={claim.replace(' ', '+')}", "snippet": f"Academic papers about {claim}", "source_type": "research"}
        ]

# ========== API ENDPOINTS ==========

@app.route('/search', methods=['POST'])
def search():
    """Search for sources related to a claim (prioritizes .edu/.gov)"""
    try:
        data = request.json
        claim = data.get('claim', '').strip()
        search_engine = data.get('search_engine', 'duckduckgo')
        ctx = data.get('context') or {}
        incident_type = data.get('incident_type') or ctx.get('claimType')
        location = data.get('location') or ctx.get('location')
        location_en = data.get('location_en') or ctx.get('location_en')
        language = data.get('language') or ctx.get('language')

        if not claim:
            return jsonify({"error": "Please provide a claim to search for"}), 400

        print(f"\n{'='*50}")
        print(f"🔍 Searching for: {claim}  (type: {incident_type or 'auto'}, loc: {location or '-'} / en: {location_en or '-'}, lang: {language or 'en'})")
        print(f"{'='*50}")

        # Perform search
        results = search_web(claim, search_engine, incident_type, location, language, location_en)

        # Never manufacture evidence for an unknown claim.
        if not results:
            print("⚠️ No real search results found")

        # Count trustworthy sources
        trusted_count = len([r for r in results if r.get('source_type') in ['government', 'education']])

        print(f"✅ Found {len(results)} sources ({trusted_count} .edu/.gov)")
        for i, source in enumerate(results[:3]):
            print(f"   {i+1}. [{source.get('source_type', 'unknown')}] {source.get('title', 'Untitled')}")
        print(f"{'='*50}\n")

        return jsonify({
            "claim": claim,
            "sources": results,
            "total_sources": len(results),
            "trusted_sources": trusted_count,
            "search_engine": search_engine,
            "agent": AGENT_NAME,
            "version": AGENT_VERSION
        })

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/search_trusted', methods=['POST'])
def search_trusted():
    """Search with .edu and .gov prioritized"""
    try:
        data = request.json
        claim = data.get('claim', '').strip()

        if not claim:
            return jsonify({"error": "Please provide a claim to search for"}), 400

        print(f"\n🔍 Searching for: {claim} (prioritizing .edu/.gov)")

        results = search_web(claim, 'duckduckgo')

        if not results:
            print("⚠️ No real trusted search results found")

        # Count .edu and .gov sources
        edu_gov = [r for r in results if r.get('source_type') in ['government', 'education']]

        return jsonify({
            "claim": claim,
            "sources": results,
            "total_sources": len(results),
            "edu_gov_sources": len(edu_gov),
            "agent": AGENT_NAME,
            "message": f"Found {len(results)} sources ({len(edu_gov)} from .edu or .gov sites)"
        })

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/engines', methods=['GET'])
def engines():
    return jsonify({
        "engines": [
            {"name": "duckduckgo", "description": "DuckDuckGo API", "requires_api_key": False},
            {"name": "wikipedia", "description": "Wikipedia API", "requires_api_key": False}
        ],
        "agent": AGENT_NAME
    })

@app.route('/health', methods=['GET'])
def health():
    critical = PROVIDER_STATUS.get("google_news", {})
    status = "degraded" if critical.get("status") == "degraded" else "alive"
    return jsonify({
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "status": status,
        "providers": PROVIDER_STATUS,
        "message": "Ready to search current authoritative feeds and news."
    })

@app.route('/info', methods=['GET'])
def info():
    return jsonify({
        "name": AGENT_NAME,
        "version": AGENT_VERSION,
        "description": "Searches the web for sources with real working URLs",
        "endpoints": {
            "search": "POST /search - Search (prioritizes .edu/.gov)",
            "search_trusted": "POST /search_trusted - Search with .edu/.gov focus",
            "health": "GET /health - Check status"
        }
    })

if __name__ == '__main__':
    print("\n" + "="*50)
    print(f"🔍 {AGENT_NAME} v{AGENT_VERSION}")
    print("="*50)
    print("📍 Running on: http://localhost:5001")
    print("🔗 Endpoint: http://localhost:5001/search")
    print("🔗 Trusted: http://localhost:5001/search_trusted")
    print("="*50)
    print("📋 Search priorities:")
    print("   1. Government sites (.gov) - HIGHEST")
    print("   2. Education sites (.edu) - HIGH")
    print("   3. Research & Fact-checking")
    print("   4. Reputable News")
    print("   5. Other sources")
    print("="*50)
    print("⚡ Ready to search!\n")

    app.run(host='0.0.0.0', port=5001, debug=False, use_reloader=False)
