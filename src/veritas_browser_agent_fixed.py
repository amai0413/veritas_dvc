from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import os
import re
import json
import socket
import ipaddress
import requests
import anthropic
from urllib.parse import urlparse
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from concurrent.futures import ThreadPoolExecutor, as_completed

# ========== ARIZE PHOENIX TRACING (opt-in) ==========
# The hosted Phoenix endpoint has been returning 500s and retrying, which adds
# latency + log noise to every request. Tracing is now OFF unless you opt in
# with ENABLE_PHOENIX=1, so the judge stays fast and quiet by default.
ENABLE_PHOENIX = os.environ.get("ENABLE_PHOENIX", "") == "1"

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ========== ARIZE / PHOENIX TRACING (opt-in) ==========
# TRACER lets us emit explicit OpenTelemetry trace "signals" for the LLM steps
# (time/location extraction + triage) so an Arize/Phoenix dashboard shows what
# the model predicted, not just that a call happened. Credentials are read from
# the environment by phoenix.otel.register():
#   PHOENIX_API_KEY, PHOENIX_COLLECTOR_ENDPOINT  (Arize Phoenix Cloud)
TRACER = None
if ENABLE_PHOENIX:
    print("🔍 Setting up Arize/Phoenix tracing...")
    _project = os.environ.setdefault("PHOENIX_PROJECT_NAME", "veritas-judge-agent")
    _api_key = os.environ.get("PHOENIX_API_KEY") or os.environ.get("ARIZE_API_KEY", "")
    _endpoint = os.environ.get("PHOENIX_COLLECTOR_ENDPOINT", "")
    try:
        from openinference.instrumentation.anthropic import AnthropicInstrumentor
        # Arize AX (otlp.arize.com) vs Arize Phoenix Cloud (app.phoenix.arize.com)
        if "otlp.arize.com" in _endpoint or os.environ.get("ARIZE_API_KEY"):
            from opentelemetry import trace as _otel_trace
            from opentelemetry.sdk.trace import TracerProvider
            from opentelemetry.sdk.trace.export import BatchSpanProcessor
            from opentelemetry.sdk.resources import Resource
            from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter
            headers = {"api_key": _api_key}
            if os.environ.get("ARIZE_SPACE_ID"):
                headers["space_id"] = os.environ["ARIZE_SPACE_ID"]
            provider = TracerProvider(resource=Resource.create(
                {"model_id": _project, "service.name": _project}))
            provider.add_span_processor(BatchSpanProcessor(
                OTLPSpanExporter(endpoint=_endpoint or "https://otlp.arize.com/v1/traces",
                                 headers=headers)))
            _otel_trace.set_tracer_provider(provider)
            AnthropicInstrumentor().instrument(tracer_provider=provider)
            TRACER = provider.get_tracer("veritas")
            print("✅ Arize AX tracing initialized!")
        else:
            from phoenix.otel import register
            tracer_provider = register(project_name=_project, auto_instrument=True)
            AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)
            TRACER = tracer_provider.get_tracer("veritas")
            print("✅ Arize Phoenix tracing initialized!")
    except Exception as e:
        print(f"⚠️ Tracing initialization warning: {e}")
        print("⚠️ Continuing without tracing...")
else:
    print("ℹ️ Arize/Phoenix tracing disabled (set ENABLE_PHOENIX=1 + PHOENIX_API_KEY/PHOENIX_COLLECTOR_ENDPOINT to enable).")

from contextlib import contextmanager

@contextmanager
def trace_span(name, **attrs):
    """Open an OTel span (no-op when tracing is disabled) and attach attributes
    as Arize/Phoenix 'signals'. Use .set_attribute on the yielded span to add
    predicted values once they're computed."""
    if TRACER is None:
        yield None
        return
    with TRACER.start_as_current_span(name) as span:
        for k, v in attrs.items():
            try:
                span.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
            except Exception:
                pass
        yield span

def _span_set(span, **attrs):
    if span is None:
        return
    for k, v in attrs.items():
        try:
            span.set_attribute(k, v if isinstance(v, (str, int, float, bool)) else str(v))
        except Exception:
            pass

# ========== AGENT ENDPOINTS ==========
SEARCHER_AGENT_URL = os.environ.get(
    "SEARCHER_AGENT_URL", "http://127.0.0.1:5001/search"
)
EXTRACTOR_AGENT_URL = os.environ.get(
    "EXTRACTOR_AGENT_URL", "http://127.0.0.1:5002/extract"
)

# ========== INITIALIZE CLAUDE ==========
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ========== MODEL TIERS ==========
# Fast/cheap model for extraction-style calls; a stronger model for the core
# crisis-triage judgment where calibration and reasoning quality matter most.
FAST_MODEL = os.environ.get("VERITAS_FAST_MODEL", "claude-haiku-4-5")
TRIAGE_MODEL = os.environ.get("VERITAS_TRIAGE_MODEL", "claude-sonnet-4-6")

# ========== AGENT IDENTITY ==========
AGENT_NAME = "Veritas Judge Agent"
AGENT_VERSION = "2.0"

# ========== SEARCH FUNCTION ==========
def search_web(claim, incident_type=None, context=None):
    """Call the Searcher Agent to find sources (routed by incident type + context)"""
    try:
        context = context or {}
        print(f"🔍 Calling Searcher Agent for: {claim} (type: {incident_type or 'auto'})")

        response = requests.post(
            SEARCHER_AGENT_URL,
            json={
                "claim": claim,
                "search_engine": "all",
                "incident_type": incident_type,
                "location": context.get("location"),
                "location_en": context.get("location_en"),
                "incident_time": context.get("incidentTime") or context.get("datetime"),
                "language": context.get("language"),
            },
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            sources = data.get('sources', [])
            print(f"✅ Searcher Agent found {len(sources)} sources")
            return sources
        else:
            print(f"⚠️ Searcher Agent returned status {response.status_code}")
            return []

    except requests.exceptions.ConnectionError:
        print("⚠️ Searcher Agent not running; returning no evidence.")
        return []
    except Exception as e:
        print(f"❌ Search error: {e}")
        return []

# ========== EXTRACT FUNCTION ==========
def extract_evidence(sources):
    """Call the Extractor Agent to get full content"""
    try:
        # Google News RSS links are JS redirects that never extract — skip them
        # (their headline snippet still reaches the judge via enrichment) and
        # spend the extraction budget on real, fetchable article URLs instead.
        extractable = [s.get('url', '') for s in sources
                       if s.get('url') and 'news.google.com' not in s.get('url', '')]
        urls = extractable[:4]

        if not urls:
            print("⚠️ No extractable URLs (snippets will be used instead)")
            return []

        print(f"📄 Calling Extractor Agent for {len(urls)} URLs")

        response = requests.post(
            EXTRACTOR_AGENT_URL,
            json={"urls": urls, "method": "basic"},
            timeout=30
        )

        if response.status_code == 200:
            data = response.json()
            evidence = data.get('evidence', [])
            print(f"✅ Extractor Agent extracted {len(evidence)} sources")
            return evidence
        else:
            print(f"⚠️ Extractor Agent returned status {response.status_code}")
            return []

    except requests.exceptions.ConnectionError:
        print("⚠️ Extractor Agent not running! Using mock data.")
        return mock_extract_evidence(sources)
    except Exception as e:
        print(f"❌ Extract error: {e}")
        return mock_extract_evidence(sources)

# ========== MOCK FUNCTIONS ==========
def mock_search_results(claim):
    """Mock search results if Searcher Agent is unavailable"""
    claim_lower = claim.lower()

    if "vaccine" in claim_lower and "autism" in claim_lower:
        return [
            {"title": "Vaccines and Autism - Wikipedia", "url": "https://en.wikipedia.org/wiki/Vaccines_and_autism", "snippet": "Multiple studies show no link between vaccines and autism.", "source_type": "encyclopedia"},
            {"title": "CDC - Vaccines and Autism", "url": "https://www.cdc.gov/vaccinesafety/concerns/autism.html", "snippet": "CDC information on vaccines and autism.", "source_type": "government"},
            {"title": "WHO - Vaccine Safety", "url": "https://www.who.int/news-room/fact-sheets/detail/vaccines-and-immunization", "snippet": "WHO confirms vaccines are safe and effective based on extensive research.", "source_type": "government"},
            {"title": "The Lancet retraction of Wakefield study", "url": "https://www.thelancet.com/journals/lancet/article/PIIS0140-6736(97)11096-0/fulltext", "snippet": "The Lancet retracted the fraudulent 1998 study that originally claimed a link between MMR vaccine and autism.", "source_type": "research"},
            {"title": "PubMed - Meta-analysis of vaccine safety", "url": "https://pubmed.ncbi.nlm.nih.gov/24814559/", "snippet": "A meta-analysis of over 1.2 million children found no association between vaccines and autism.", "source_type": "research"},
        ]
    elif "earth" in claim_lower and "sun" in claim_lower:
        return [
            {"title": "Earth's Orbit - Wikipedia", "url": "https://en.wikipedia.org/wiki/Earth%27s_orbit", "snippet": "The Earth orbits the Sun at 67,000 mph.", "source_type": "encyclopedia"},
            {"title": "NASA - Earth's Orbit", "url": "https://www.nasa.gov/audience/forstudents/5-8/features/nasa-knows/what-is-orbit-58.html", "snippet": "NASA explains Earth's orbit around the Sun.", "source_type": "government"},
            {"title": "ESA - Solar System", "url": "https://www.esa.int/Science_Exploration/Space_Science/Solar_System", "snippet": "The European Space Agency describes Earth's orbital mechanics around the Sun.", "source_type": "government"},
            {"title": "Nature - Orbital Dynamics", "url": "https://www.nature.com/subjects/orbital-mechanics", "snippet": "Peer-reviewed research on planetary orbital dynamics and Earth-Sun interactions.", "source_type": "research"},
            {"title": "Sky & Telescope - Earth's Orbit Explained", "url": "https://skyandtelescope.org/astronomy-resources/earths-orbit/", "snippet": "Accessible explanation of Earth's elliptical orbit and seasonal variations.", "source_type": "general"},
        ]
    elif "climate" in claim_lower and "change" in claim_lower:
        return [
            {"title": "Climate Change - Wikipedia", "url": "https://en.wikipedia.org/wiki/Climate_change", "snippet": "Scientific consensus confirms climate change.", "source_type": "encyclopedia"},
            {"title": "NASA Climate Change Evidence", "url": "https://climate.nasa.gov/evidence/", "snippet": "NASA's evidence for climate change.", "source_type": "government"},
            {"title": "IPCC Sixth Assessment Report", "url": "https://www.ipcc.ch/assessment-report/ar6/", "snippet": "The IPCC confirms that human influence has warmed the climate at a rate unprecedented in at least 2,000 years.", "source_type": "research"},
            {"title": "NOAA Climate.gov", "url": "https://www.climate.gov/", "snippet": "NOAA's climate monitoring data shows consistent warming trends across all major indicators.", "source_type": "government"},
            {"title": "Nature - Climate Science", "url": "https://www.nature.com/nclimate/", "snippet": "Peer-reviewed climate research and analysis from Nature Climate Change journal.", "source_type": "research"},
        ]
    elif "hackathon" in claim_lower:
        return [
            {"title": "Hackathon - Wikipedia", "url": "https://en.wikipedia.org/wiki/Hackathon", "snippet": "A hackathon is an event where people engage in collaborative computer programming.", "source_type": "encyclopedia"},
            {"title": "Major League Hacking", "url": "https://mlh.io/", "snippet": "Official resource for hackathon participants.", "source_type": "general"},
            {"title": "Devpost - Hackathon Directory", "url": "https://devpost.com/hackathons", "snippet": "Devpost hosts project submissions and results from hackathons worldwide.", "source_type": "general"},
            {"title": "TechCrunch - Hackathon Coverage", "url": "https://techcrunch.com/tag/hackathon/", "snippet": "TechCrunch covers major hackathon events and winning projects.", "source_type": "news"},
            {"title": "GitHub - Hackathon Resources", "url": "https://github.com/topics/hackathon", "snippet": "Open-source hackathon projects, tools, and starter kits on GitHub.", "source_type": "general"},
        ]
    else:
        return [
            {"title": f"Wikipedia: {claim}", "url": f"https://en.wikipedia.org/wiki/{claim.replace(' ', '_')}", "snippet": f"Wikipedia article about {claim}.", "source_type": "encyclopedia"},
            {"title": f"Google Scholar search for: {claim}", "url": f"https://scholar.google.com/scholar?q={claim.replace(' ', '+')}", "snippet": f"Academic papers about {claim}.", "source_type": "research"},
            {"title": f"Reuters Fact Check: {claim}", "url": f"https://www.reuters.com/fact-check/search/{claim.replace(' ', '+')}", "snippet": f"Reuters fact-checking coverage related to {claim}.", "source_type": "news"},
            {"title": f"Associated Press: {claim}", "url": f"https://apnews.com/search?q={claim.replace(' ', '+')}", "snippet": f"AP News coverage and reporting on {claim}.", "source_type": "news"},
            {"title": f"Snopes: {claim}", "url": f"https://www.snopes.com/?s={claim.replace(' ', '+')}", "snippet": f"Snopes fact-check and analysis of claims related to {claim}.", "source_type": "general"},
        ]

def mock_extract_evidence(sources):
    """Mock evidence if Extractor Agent is unavailable"""
    evidence = []
    for source in sources[:3]:
        evidence.append({
            "title": source.get("title", "Source"),
            "url": source.get("url", ""),
            "content": source.get("snippet", "No content available")
        })
    return evidence

def make_excerpt(content, claim, limit=260):
    """Pick the most claim-relevant passage from content for display."""
    text = re.sub(r"\s+", " ", (content or "")).strip()
    if not text:
        return ""
    # split into sentences and score by overlap with claim keywords
    keywords = {w.lower() for w in re.findall(r"[A-Za-z0-9]{4,}", claim)}
    sentences = re.split(r"(?<=[.!?])\s+", text)
    best, best_score = None, -1
    for s in sentences:
        score = sum(1 for w in keywords if w in s.lower())
        if score > best_score and len(s) > 20:
            best, best_score = s, score
    chosen = best if (best and best_score > 0) else text
    return chosen[:limit].strip() + ("…" if len(chosen) > limit else "")

def enrich_evidence_with_snippets(search_results, evidence):
    """Guarantee the judge sees the search snippets (authoritative USGS data,
    news headlines, etc.) even when full-page extraction is thin or fails.
    Falls back to the snippet when extracted content is short."""
    by_url = {e.get("url", ""): e for e in (evidence or [])}
    merged = []
    for s in search_results[:16]:  # full candidate pool; select_evidence trims it
        url = s.get("url", "")
        snippet = (s.get("snippet", "") or "").strip()
        e = by_url.get(url)
        content = ((e.get("content", "") if e else "") or "").strip()
        # If the scraped page gave us little, lead with the snippet.
        if len(content) < 200 and snippet:
            content = (snippet + "\n" + content).strip()
        title = (e.get("title") if e and e.get("title") and e.get("title") != "None"
                 else s.get("title", "Source"))
        merged.append({
            "title": title,
            "url": url,
            "content": content or snippet or s.get("title", ""),
            "source_type": s.get("source_type", "general"),
            "published": s.get("published", ""),
            "publisher": s.get("publisher", ""),
            "provider": s.get("provider", ""),
            "relevance_score": s.get("relevance_score"),
            "freshness_score": s.get("freshness_score"),
            "rank_score": s.get("rank_score"),
        })
    return merged

# ========== JSON PARSING HELPER ==========
def parse_judge_json(text):
    """Parse the model's JSON response, tolerating markdown code fences and extra text."""
    import re

    cleaned = text.strip()

    # Strip ```json ... ``` or ``` ... ``` fences if present
    fence_match = re.search(r"```(?:json)?\s*(.*?)```", cleaned, re.DOTALL)
    if fence_match:
        cleaned = fence_match.group(1).strip()

    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        # Fall back to extracting the first {...} object in the text
        obj_match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if obj_match:
            return json.loads(obj_match.group(0))
        raise

# ========== CONTEXT EXTRACTION (location / datetime / type from the claim) ==========
VALID_CLAIM_TYPES = {
    # natural hazards
    "earthquake", "wildfire", "flood_storm", "volcano", "other_disaster",
    # violence & security
    "shooting", "terrorism", "violent_crime", "conflict", "protest",
    # emergency response
    "rescue_request", "shelter_status", "infrastructure", "donation_aid",
    # other
    "misinformation", "other",
}

CONTEXT_TYPE_KEYWORDS = (
    ("earthquake", ("earthquake", "quake", "aftershock", "seismic", "tremor")),
    ("wildfire", ("wildfire", "bushfire", "forest fire", "brush fire")),
    ("flood_storm", ("flood", "flooding", "hurricane", "typhoon", "cyclone",
                     "storm", "tornado", "tsunami")),
    ("volcano", ("volcano", "volcanic", "eruption", "lava", "ash cloud")),
    ("shooting", ("shooting", "shooter", "gunfire")),
    ("terrorism", ("terrorist", "terrorism", "bombing", "bomb attack")),
    ("violent_crime", ("murder", "homicide", "stabbing", "kidnapping")),
    ("conflict", ("war", "airstrike", "missile strike", "armed conflict", "invasion")),
    ("protest", ("protest", "demonstration", "riot", "civil unrest")),
    ("rescue_request", ("rescue needed", "needs rescue", "trapped", "stranded")),
    ("shelter_status", ("evacuation center", "evacuation centre", "shelter is open",
                        "shelter opened", "accepting families")),
    ("infrastructure", ("bridge", "road closure", "road closed", "collapsed",
                        "impassable", "power outage", "blackout")),
    ("donation_aid", ("donate", "donation", "relief fund", "aid collection")),
    ("other_disaster", ("heat wave", "heatwave", "extreme heat", "cold wave",
                        "drought", "landslide", "avalanche", "emergency")),
)

CONTEXT_LOCATION_STOPWORDS = {
    "a", "an", "the", "this", "that", "these", "those", "breaking", "reports",
    "report", "officials", "authorities", "government", "magnitude", "heat",
    "wave", "earthquake", "wildfire", "flood", "storm", "hurricane", "typhoon",
    "cyclone", "volcano", "evacuation", "shelter", "magnitude", "public", "outdoor",
}

# Used only as a conservative fallback for claims such as "France restricted..."
# where the place is the grammatical subject rather than following "in/near/at".
# Keeping this explicit prevents arbitrary first words such as "Authorities" or
# "Breaking" from being treated as locations.
COUNTRY_AND_TERRITORY_NAMES = {
    name.strip().lower()
    for name in """
Afghanistan, Albania, Algeria, Andorra, Angola, Antigua and Barbuda, Argentina,
Armenia, Australia, Austria, Azerbaijan, Bahamas, Bahrain, Bangladesh, Barbados,
Belarus, Belgium, Belize, Benin, Bhutan, Bolivia, Bosnia and Herzegovina,
Botswana, Brazil, Brunei, Bulgaria, Burkina Faso, Burundi, Cabo Verde, Cambodia,
Cameroon, Canada, Central African Republic, Chad, Chile, China, Colombia,
Comoros, Costa Rica, Croatia, Cuba, Cyprus, Czech Republic, Czechia,
Democratic Republic of the Congo, Denmark, Djibouti, Dominica,
Dominican Republic, East Timor, Timor-Leste, Ecuador, Egypt, El Salvador,
Equatorial Guinea, Eritrea, Estonia, Eswatini, Ethiopia, Fiji, Finland, France,
Gabon, Gambia, Georgia, Germany, Ghana, Greece, Grenada, Guatemala, Guinea,
Guinea-Bissau, Guyana, Haiti, Honduras, Hungary, Iceland, India, Indonesia,
Iran, Iraq, Ireland, Israel, Italy, Ivory Coast, Côte d'Ivoire, Jamaica, Japan,
Jordan, Kazakhstan, Kenya, Kiribati, Kosovo, Kuwait, Kyrgyzstan, Laos, Latvia,
Lebanon, Lesotho, Liberia, Libya, Liechtenstein, Lithuania, Luxembourg,
Madagascar, Malawi, Malaysia, Maldives, Mali, Malta, Marshall Islands,
Mauritania, Mauritius, Mexico, Micronesia, Moldova, Monaco, Mongolia,
Montenegro, Morocco, Mozambique, Myanmar, Namibia, Nauru, Nepal, Netherlands,
New Zealand, Nicaragua, Niger, Nigeria, North Korea, North Macedonia, Norway,
Oman, Pakistan, Palau, Palestine, Panama, Papua New Guinea, Paraguay, Peru,
Philippines, Poland, Portugal, Qatar, Republic of the Congo, Romania, Russia,
Rwanda, Saint Kitts and Nevis, Saint Lucia, Saint Vincent and the Grenadines,
Samoa, San Marino, Sao Tome and Principe, Saudi Arabia, Senegal, Serbia,
Seychelles, Sierra Leone, Singapore, Slovakia, Slovenia, Solomon Islands,
Somalia, South Africa, South Korea, South Sudan, Spain, Sri Lanka, Sudan,
Suriname, Sweden, Switzerland, Syria, Taiwan, Tajikistan, Tanzania, Thailand,
Togo, Tonga, Trinidad and Tobago, Tunisia, Turkey, Türkiye, Turkmenistan,
Tuvalu, Uganda, Ukraine, United Arab Emirates, United Kingdom, United States,
Uruguay, Uzbekistan, Vanuatu, Vatican City, Venezuela, Vietnam, Yemen, Zambia,
Zimbabwe, England, Scotland, Wales, Northern Ireland, Hong Kong, Macau,
Puerto Rico, Greenland, French Polynesia, New Caledonia, Guam
""".replace("\n", " ").split(",")
    if name.strip()
}

LOCATION_SUFFIXES = {
    "city", "province", "state", "county", "district", "region", "island",
    "islands", "coast", "valley", "mountain", "river", "bay", "peninsula",
    "prefecture", "municipality", "barangay", "village", "town", "airport",
    "school", "elementary", "hospital", "bridge", "road", "street", "st",
}

LOCATION_DIRECTION_WORDS = (
    "northern", "southern", "eastern", "western", "central", "north", "south",
    "east", "west", "northeast", "northwest", "southeast", "southwest",
    "north-east", "north-west", "south-east", "south-west", "coastal",
)

def _clean_location_candidate(value):
    value = re.sub(r"\s+", " ", (value or "")).strip(" ,.;:()[]")
    value = re.sub(
        r"\s+\b(?:is|are|was|were|has|have|had|where|after|before|during)\b.*$",
        "",
        value,
        flags=re.IGNORECASE,
    )
    return value.strip(" ,.;:")

def _find_country_mentions(text):
    matches = []
    for name in COUNTRY_AND_TERRITORY_NAMES:
        match = re.search(rf"(?<!\w){re.escape(name)}(?!\w)", text, re.IGNORECASE)
        if match:
            matches.append((match.start(), -(match.end() - match.start()), match.group(0)))
    return [item[2] for item in sorted(matches)]

def _is_plausible_subject_location(candidate):
    lower = candidate.lower()
    if lower in COUNTRY_AND_TERRITORY_NAMES:
        return True
    words = re.findall(r"[A-Za-zÀ-ÖØ-öø-ÿ.'’-]+", lower)
    return bool(words and words[-1].rstrip(".") in LOCATION_SUFFIXES)

def extract_context_fallback(claim):
    """Best-effort extraction that still works when the AI service is unavailable."""
    text = (claim or "").strip()
    lower = text.lower()

    claim_type = "other"
    for candidate, keywords in CONTEXT_TYPE_KEYWORDS:
        if any(re.search(rf"(?<!\w){re.escape(keyword)}(?!\w)", lower)
               for keyword in keywords):
            claim_type = candidate
            break

    # Preserve explicit time expressions only; never invent a date.
    time_patterns = (
        r"\b(?:today|tonight|yesterday|tomorrow|this (?:morning|afternoon|evening|weekend))\b",
        r"\b(?:about\s+|around\s+|roughly\s+)?\d+\s+"
        r"(?:minutes?|hours?|days?|weeks?|months?)\s+ago\b",
        r"\b(?:on\s+)?(?:Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
        r"(?:\s+(?:morning|afternoon|evening|night))?\b",
        r"\b(?:on\s+)?(?:January|February|March|April|May|June|July|August|"
        r"September|October|November|December)\s+\d{1,2}(?:,\s*\d{4})?\b",
        r"\b\d{4}-\d{2}-\d{2}\b",
        r"\b(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:a\.?m\.?|p\.?m\.?|UTC|GMT)\b",
    )
    incident_time = None
    for pattern in time_patterns:
        match = re.search(pattern, text, re.IGNORECASE)
        if match:
            incident_time = match.group(0).strip()
            break

    location = None
    proper_word = r"[A-ZÀ-ÖØ-Þ][\wÀ-ÖØ-öø-ÿ.'’-]*"
    connector = r"(?:and|of the|of|de la|de|del|la|las|los|da|do|dos|di|du)"
    proper_phrase = rf"{proper_word}(?:\s+{proper_word}|\s+{connector}\s+{proper_word}){{0,5}}"
    direction_word = rf"(?:{'|'.join(LOCATION_DIRECTION_WORDS)})"
    directional = rf"(?:{direction_word}\s+)?"
    named_place = rf"{proper_phrase}(?:\s*,\s*{proper_phrase}){{0,2}}"
    # Preserve relative geography instead of collapsing it to the country:
    # "southwest of Crete, Greece", "off the coast of Japan", etc.
    relative_place = (
        rf"(?:{direction_word}\s+of\s+{named_place}|"
        rf"off\s+the\s+coast\s+of\s+{named_place})"
    )
    location_phrase = rf"(?:{relative_place}|{directional}{named_place})"

    # Strongest signal: preserve an explicit relative-geography phrase in full.
    relative = re.search(rf"\b({relative_place})", text)
    if relative:
        location = _clean_location_candidate(relative.group(1))
    else:
        # Explicit location prepositions: "in southern France", "near Tokyo",
        # and "at San Roque Elementary".
        prep = re.search(
            rf"\b(?:at|in|near|across|throughout|outside|around)\s+"
            rf"({location_phrase})",
            text,
        )
        if prep:
            location = _clean_location_candidate(prep.group(1))
        else:
            # Event wording commonly places the location after a verb.
            relation = re.search(
                rf"\b(?:hit|hits|struck|reached|approached|affected|devastated|"
                rf"landed in|made landfall in)\s+"
                rf"({location_phrase})",
                text,
            )
            if relation:
                location = _clean_location_candidate(relation.group(1))

    if not location:
        # A country/territory can appear anywhere without a preposition.
        countries = _find_country_mentions(text)
        if countries:
            location = countries[0]

    if not location:
        # Finally accept a leading subject only when it is a known country or
        # carries an explicit place suffix. Never accept an arbitrary first word.
        leading = re.match(rf"^\s*({proper_phrase})\b", text)
        if leading:
            candidate = _clean_location_candidate(leading.group(1))
            if (candidate.lower() not in CONTEXT_LOCATION_STOPWORDS
                    and _is_plausible_subject_location(candidate)):
                location = candidate

    return {"location": location, "location_en": location, "datetime": incident_time,
            "claim_type": claim_type, "language": "en"}

def extract_context(claim):
    """Time/location/type extraction, traced to Arize/Phoenix as a span whose
    attributes are the predicted values (the 'signals' a judge can inspect)."""
    with trace_span("extract_context.time_location", **{"input.claim": (claim or "")[:500]}) as span:
        result = _extract_context_impl(claim)
        _span_set(span,
                  **{"predicted.location": str(result.get("location")),
                     "predicted.location_en": str(result.get("location_en")),
                     "predicted.datetime": str(result.get("datetime")),
                     "predicted.claim_type": str(result.get("claim_type")),
                     "predicted.language": str(result.get("language"))})
        return result

def _extract_context_impl(claim):
    """Use Claude to read the incident location, date/time, and type from the claim text."""
    fallback = extract_context_fallback(claim)
    if not ANTHROPIC_API_KEY:
        return fallback

    prompt = f"""Extract structured fields from this crisis/incident claim. Return ONLY a JSON object.

CLAIM: "{claim}"

Return exactly this shape:
{{"location": "<place as written in the claim's language>", "location_en": "<same place in English, e.g. 'Crete, Greece'>", "datetime": "<when it happened or was reported, copied as written>", "claim_type": "<one incident type from the list below>", "language": "<ISO 639-1 code of the claim's language, e.g. 'en','ja','es'>"}}

claim_type must be exactly one of:
- earthquake, wildfire, flood_storm, volcano, other_disaster
- shooting, terrorism, violent_crime, conflict, protest
- rescue_request, shelter_status, infrastructure, donation_aid
- misinformation, other

Rules:
- If the location is not stated, set location and location_en to null.
- location_en is the English name of the place (translate/transliterate) so it can be matched against English data feeds; if already English, repeat it.
- If no date/time is stated, set datetime to null.
- language is the language the CLAIM is written in (not the location's language).
- Choose the single best claim_type; use "other" if none clearly fits.
- Output ONLY the JSON object, nothing else."""
    try:
        resp = claude.messages.create(
            model=FAST_MODEL,
            max_tokens=200,
            temperature=0,
            messages=[{"role": "user", "content": prompt}]
        )
        data = parse_judge_json(resp.content[0].text)
    except Exception as e:
        print(f"⚠️ extract_context error: {type(e).__name__}: {e}", flush=True)
        return fallback

    def clean(v):
        if not isinstance(v, str):
            return None
        v = v.strip()
        return None if v.lower() in ("", "null", "none", "n/a", "unknown") else v

    ctype = data.get("claim_type") or "other"
    if ctype not in VALID_CLAIM_TYPES:
        ctype = "other"
    lang = clean(data.get("language"))
    lang = lang.lower()[:5] if lang else "en"
    return {
        "location": clean(data.get("location")) or fallback["location"],
        "location_en": clean(data.get("location_en")) or clean(data.get("location")) or fallback["location"],
        "datetime": clean(data.get("datetime")) or fallback["datetime"],
        "claim_type": ctype if ctype != "other" else fallback["claim_type"],
        "language": lang,
    }

# ========== JUDGE FUNCTION WITH CLAUDE ==========
def judge_claim(claim, evidence):
    """Analyze evidence and score the claim using Claude AI"""
    if not ANTHROPIC_API_KEY:
        print("ℹ️ No ANTHROPIC_API_KEY; using deterministic evidence scorer.", flush=True)
        return judge_fallback(claim, evidence)

    # Build evidence text
    evidence_text = ""
    if evidence:
        for i, e in enumerate(evidence):
            content = e.get('content', '')
            if content and len(content) > 50:
                evidence_text += (
                    f"\nSource {i+1}: {e.get('title', 'Untitled')}\n"
                    f"Type: {e.get('source_type', 'unknown')}\n"
                    f"Published: {e.get('published') or 'unknown'}\n"
                    f"Searcher relevance: {e.get('relevance_score')}\n"
                    f"Searcher freshness: {e.get('freshness_score')}\n"
                    f"Content: {content[:1500]}\n"
                )
    else:
        evidence_text = "No specific evidence found."

    # Build the prompt for Claude
    prompt = f"""You are Veritas, an AI truth-judge. Evaluate this claim based on the provided evidence.

CLAIM: "{claim}"

EVIDENCE:
{evidence_text}

Use only the supplied evidence. For claims about current disasters or recent
events, prioritize newer reports and authoritative feeds. Distinguish:
- direct support for the exact location, time, severity, and status;
- contradiction of a concrete detail;
- merely related background that neither supports nor contradicts.
Do not treat a search page or a source repeating the query as confirmation.

Based on the evidence, provide:
1. A SCORE from 0-100
   - 0-30 = False (claim is clearly false)
   - 31-69 = Disputed/Uncertain (claim is contested or unclear)
   - 70-100 = Verified (claim is clearly true)

2. REASONING (2-3 sentences explaining your score)

3. STATUS (choose exactly one: Verified, False, Disputed, or Uncertain)

Return ONLY valid JSON in this format:
{{"score": 75, "reasoning": "The evidence supports this claim because...", "status": "Verified"}}

If evidence is missing, stale, weakly related, or does not establish the exact
claim, use Uncertain rather than guessing. Output ONLY the JSON object.
"""

    try:
        print("🧠 Sending to Claude for analysis...", flush=True)
        print(f"🔑 ANTHROPIC_API_KEY set: {bool(ANTHROPIC_API_KEY)} len={len(ANTHROPIC_API_KEY)}", flush=True)

        # FIXED: Using the correct Claude model name
        response = claude.messages.create(
            model=FAST_MODEL,
            max_tokens=500,
            temperature=0.3,
            messages=[
                {"role": "user", "content": prompt}
            ]
        )

        result_text = response.content[0].text
        print(f"📝 Claude response: {result_text}", flush=True)

        result = parse_judge_json(result_text)

        score = max(0, min(100, int(result.get("score", 50))))
        reasoning = result.get("reasoning", "Analysis complete.")
        status = result.get("status", "Uncertain")

        valid_statuses = ["Verified", "False", "Disputed", "Uncertain"]
        if status not in valid_statuses:
            status = "Uncertain"

        return {
            "score": score,
            "reasoning": reasoning,
            "status": status,
            # Keep per-source scoring deterministic so the graph remains
            # explainable even when Claude supplies the overall verdict.
            "evidence_assessments": [
                assess_evidence_item(claim, item, extract_context_fallback(claim))
                for item in evidence or []
            ],
        }

    except Exception as e:
        print(f"❌ Claude error: {type(e).__name__}: {e}", flush=True)
        return judge_fallback(claim, evidence)

# ========== FALLBACK JUDGE ==========
SPORTS_TERM_CORRECTIONS = {
    r"\btsunizia\b": "tunisia",
    r"\btunizia\b": "tunisia",
    r"\bworldcup\b": "world cup",
}

def normalize_sports_text(text):
    normalized = re.sub(r"\s+", " ", (text or "")).strip().lower()
    for pattern, replacement in SPORTS_TERM_CORRECTIONS.items():
        normalized = re.sub(pattern, replacement, normalized, flags=re.IGNORECASE)
    return normalized

def parse_sports_result_claim(claim):
    text = normalize_sports_text(claim)
    match = re.match(
        r"^\s*(.+?)\s+(won|lost|beat|defeated)\s+(?:against|to)?\s*(.+?)"
        r"(?:\s+(?:for|in|at)\s+(?:the\s+)?world cup)?\s*$",
        text,
    )
    if not match:
        return None
    subject = match.group(1).strip(" ,.")
    verb = match.group(2)
    opponent = match.group(3).strip(" ,.")
    claimed_winner = opponent if verb == "lost" else subject
    return {"subject": subject, "opponent": opponent, "winner": claimed_winner}

def infer_match_winner(text, first_team, second_team):
    text = normalize_sports_text(text)
    a, b = re.escape(first_team), re.escape(second_team)

    for left, right in ((first_team, second_team), (second_team, first_team)):
        pattern = rf"\b{re.escape(left)}\s+(\d+)\s*[-–:]\s*(\d+)\s+{re.escape(right)}\b"
        match = re.search(pattern, text)
        if match:
            left_score, right_score = int(match.group(1)), int(match.group(2))
            if left_score != right_score:
                return left if left_score > right_score else right

    winner_patterns = (
        rf"\b({a}|{b})(?:['’]s)?\s+\d+\s*[-–]\s*\d+\s+win\s+(?:over|against)\s+({a}|{b})\b",
        rf"\b({a}|{b}).{{0,45}}\b(?:defeated|beat|beats|thrashed|routed|"
        rf"knocked|knocks|knock|eliminated|eliminates|eliminate)\s+({a}|{b})\b",
    )
    for pattern in winner_patterns:
        match = re.search(pattern, text)
        if match and match.group(1) != match.group(2):
            return match.group(1)

    loser_pattern = rf"\b({a}|{b}).{{0,30}}\b(?:lost|loss|defeat)\s+(?:to|against)\s+({a}|{b})\b"
    match = re.search(loser_pattern, text)
    if match and match.group(1) != match.group(2):
        return match.group(2)
    return None

def judge_sports_result(claim, evidence):
    parsed = parse_sports_result_claim(claim)
    if not parsed:
        return None

    trust = {
        "government": 1.0, "education": 0.9, "research": 0.85,
        "fact_check": 0.9, "news_reputable": 0.9, "news": 0.75,
        "encyclopedia": 0.65, "general": 0.45, "unknown": 0.35,
    }
    support_mass = contradiction_mass = 0.0
    matched_sources = 0
    observed_winners = []
    assessments = []
    for item in evidence or []:
        source_text = " ".join((item.get("title", "") or "", item.get("content", "") or ""))
        winner = infer_match_winner(source_text, parsed["subject"], parsed["opponent"])
        if not winner:
            continue
        matched_sources += 1
        observed_winners.append(winner.title())
        weight = trust.get(item.get("source_type", "unknown"), 0.4)
        supports = winner == parsed["winner"]
        if supports:
            support_mass += weight
        else:
            contradiction_mass += weight
        assessments.append({
            "url": item.get("url", ""),
            "title": item.get("title", "Source"),
            "stance": round(0.92 if supports else -0.92, 3),
            "relevance": 1.0,
            "freshness": round(evidence_freshness(item, current_claim=True), 3),
            "trust": round(weight, 3),
            "weight": round(weight * evidence_freshness(item, current_claim=True), 4),
            "support_reasons": ["match_winner"] if supports else [],
            "contradiction_reasons": [] if supports else ["match_winner"],
        })

    total = support_mass + contradiction_mass
    if total == 0:
        return {
            "score": 30,
            "reasoning": (
                "No source with a concrete score or unambiguous match result was found. "
                "The claim cannot be verified from the available evidence."
            ),
            "status": "Uncertain",
            "evidence_assessments": assessments,
        }

    support_ratio = support_mass / total
    if support_ratio >= 0.67:
        score, status = min(95, round(72 + support_ratio * 23)), "Verified"
        conclusion = "The reported match result supports the claim."
    elif support_ratio <= 0.33:
        score, status = max(5, round(28 * support_ratio)), "False"
        conclusion = "The reported match result contradicts the claim."
    else:
        score, status = 50, "Disputed"
        conclusion = "The available match reports conflict."

    winner_summary = ", ".join(sorted(set(observed_winners)))
    return {
        "score": score,
        "reasoning": (
            f"{matched_sources} source(s) provided a concrete result; "
            f"the observed winner was {winner_summary}. {conclusion}"
        ),
        "status": status,
        "evidence_assessments": assessments,
    }

EVIDENCE_TRUST = {
    "government": 1.0, "education": 0.94, "research": 0.88,
    "fact_check": 0.9, "news_reputable": 0.86, "news": 0.68,
    "encyclopedia": 0.55, "general": 0.4, "unknown": 0.3,
}

JUDGE_STOPWORDS = {
    "about", "after", "again", "against", "also", "and", "are", "as", "at",
    "be", "been", "before", "but", "by", "for", "from", "had", "has", "have",
    "in", "into", "is", "it", "its", "near", "of", "on", "or", "some",
    "that", "the", "their", "there", "this", "to", "toward", "was", "were",
    "with", "reports", "report", "immediate",
}

STATE_CONCEPTS = {
    "damage": ("damage", "damaged", "destruction"),
    "casualties": ("casualties", "casualty", "injuries", "injured", "deaths", "dead", "killed"),
    "tsunami_warning": ("tsunami warning", "tsunami alert"),
    "evacuation": ("evacuation", "evacuated", "evacuate"),
    "open": ("open", "opened", "accepting"),
    "closed": ("closed", "closure", "shut"),
    "collapsed": ("collapsed", "collapse"),
    "passable": ("passable", "impassable"),
}

ANTONYM_STATES = (
    (("open", "opened", "accepting"), ("closed", "closure", "shut")),
    (("passable",), ("impassable", "blocked")),
    (("standing", "intact"), ("collapsed", "collapse", "destroyed")),
)

def parse_evidence_datetime(value):
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
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
        except Exception:
            return None

def evidence_freshness(item, current_claim=True):
    supplied = item.get("freshness_score")
    if supplied is not None:
        try:
            return max(0.05, min(1.0, float(supplied)))
        except (TypeError, ValueError):
            pass
    published = parse_evidence_datetime(item.get("published", ""))
    if not published:
        return 0.7 if item.get("source_type") == "government" else 0.3
    if not current_claim:
        return 0.75
    age_hours = max(0.0, (datetime.now(timezone.utc) - published).total_seconds() / 3600)
    if age_hours <= 24:
        return 1.0
    if age_hours <= 72:
        return 0.9
    if age_hours <= 24 * 7:
        return 0.78
    if age_hours <= 24 * 30:
        return 0.58
    if age_hours <= 24 * 180:
        return 0.3
    return 0.1

def normalized_tokens(text):
    return {
        token for token in re.findall(r"[a-z0-9]+", (text or "").lower())
        if len(token) >= 3 and token not in JUDGE_STOPWORDS
    }

def concept_state(text, aliases):
    lowered = (text or "").lower()
    found = False
    state = 0
    for alias in aliases:
        for match in re.finditer(rf"(?<!\w){re.escape(alias)}(?!\w)", lowered):
            found = True
            before = lowered[max(0, match.start() - 35):match.start()]
            after = lowered[match.end():match.end() + 18]
            negated = bool(re.search(
                r"(?:\bno\b(?:\s+\w+){0,4}|"
                r"\bnot\b(?:\s+\w+){0,3}|"
                r"\bwithout\b(?:\s+\w+){0,3}|"
                r"\bzero\b|\bnone\b)\s*$",
                before,
            ))
            if alias == "impassable":
                state = -1
            elif negated or re.match(r"\s+(?:was|were)?\s*not\b", after):
                state = -1
            else:
                state = 1
    return state if found else 0

def extract_numeric_facts(text):
    lowered = (text or "").lower()
    facts = {}
    patterns = {
        "magnitude": r"(?:magnitude|mag(?:nitude)?|m)\s*[-:]?\s*(\d(?:\.\d+)?)",
        "temperature_c": r"(\d{2}(?:\.\d+)?)\s*°?\s*c\b",
        "deaths": r"(\d+)\s+(?:people\s+)?(?:dead|deaths?|killed)",
        "injuries": r"(\d+)\s+(?:people\s+)?(?:injured|injuries)",
    }
    for key, pattern in patterns.items():
        match = re.search(pattern, lowered)
        if match:
            facts[key] = float(match.group(1))
    return facts

def assess_evidence_item(claim, item, context):
    text = " ".join((item.get("title", "") or "", item.get("content", "") or ""))
    claim_token_set = normalized_tokens(claim)
    text_token_set = normalized_tokens(text)
    overlap = len(claim_token_set & text_token_set) / max(1, min(len(claim_token_set), 12))
    relevance = item.get("relevance_score")
    try:
        relevance = float(relevance)
    except (TypeError, ValueError):
        relevance = overlap

    location = context.get("location")
    location_match = None
    if location:
        location_words = [
            word for word in re.findall(r"[a-z0-9]+", location.lower())
            if len(word) >= 3 and word not in {"north", "south", "east", "west"}
        ]
        location_match = bool(location_words) and any(word in text.lower() for word in location_words)
        relevance += 0.18 if location_match else -0.2

    claim_type = context.get("claim_type", "other")
    type_keywords = dict(CONTEXT_TYPE_KEYWORDS).get(claim_type, ())
    type_match = bool(type_keywords) and any(keyword in text.lower() for keyword in type_keywords)
    if type_keywords:
        relevance += 0.14 if type_match else -0.16
    relevance = max(0.0, min(1.0, relevance))

    contradiction_reasons = []
    support_reasons = []
    claimed_state_concepts = set()
    supported_state_concepts = set()
    for concept, aliases in STATE_CONCEPTS.items():
        claim_state = concept_state(claim, aliases)
        evidence_state = concept_state(text, aliases)
        if claim_state:
            claimed_state_concepts.add(concept)
        if claim_state and evidence_state:
            if claim_state == evidence_state:
                support_reasons.append(concept)
                supported_state_concepts.add(concept)
            else:
                contradiction_reasons.append(concept)
    claim_lower = claim.lower()
    evidence_lower = text.lower()
    for positive_aliases, negative_aliases in ANTONYM_STATES:
        claim_positive = any(alias in claim_lower for alias in positive_aliases)
        claim_negative = any(alias in claim_lower for alias in negative_aliases)
        evidence_positive = any(alias in evidence_lower for alias in positive_aliases)
        evidence_negative = any(alias in evidence_lower for alias in negative_aliases)
        if (claim_positive and evidence_negative) or (claim_negative and evidence_positive):
            contradiction_reasons.append(f"{positive_aliases[0]}_state")
        elif (claim_positive and evidence_positive) or (claim_negative and evidence_negative):
            support_reasons.append(f"{positive_aliases[0]}_state")
            supported_state_concepts.add(f"{positive_aliases[0]}_state")

    claim_numbers = extract_numeric_facts(claim)
    evidence_numbers = extract_numeric_facts(text)
    tolerances = {"magnitude": 0.25, "temperature_c": 2.0, "deaths": 0.0, "injuries": 0.0}
    for key, claim_value in claim_numbers.items():
        if key not in evidence_numbers:
            continue
        if abs(claim_value - evidence_numbers[key]) <= tolerances[key]:
            support_reasons.append(key)
        else:
            contradiction_reasons.append(key)

    direct_match = bool(support_reasons or (location_match is not False and type_match and relevance >= 0.42))
    if contradiction_reasons:
        stance = -(0.45 + relevance * 0.5)
    elif claimed_state_concepts and not supported_state_concepts:
        # Matching the event, place, or magnitude is only partial evidence when
        # the claim also asserts damage/casualties/closure/etc.
        stance = 0.05 + relevance * 0.18
    elif direct_match:
        stance = 0.35 + relevance * 0.6
    elif relevance >= 0.32:
        stance = 0.12 + relevance * 0.25
    else:
        stance = 0.0
    stance = max(-1.0, min(1.0, stance))

    trust = EVIDENCE_TRUST.get(item.get("source_type", "unknown"), 0.3)
    mentioned_years = [int(year) for year in re.findall(r"\b(?:19\d{2}|20\d{2})\b", claim)]
    current_claim = not any(year < datetime.now(timezone.utc).year for year in mentioned_years)
    freshness = evidence_freshness(item, current_claim=current_claim)
    weight = trust * relevance * freshness
    return {
        "url": item.get("url", ""),
        "title": item.get("title", "Source"),
        "stance": round(stance, 3),
        "relevance": round(relevance, 3),
        "freshness": round(freshness, 3),
        "trust": round(trust, 3),
        "weight": round(weight, 4),
        "support_reasons": support_reasons,
        "contradiction_reasons": contradiction_reasons,
    }

def judge_evidence_deterministically(claim, evidence):
    context = extract_context_fallback(claim)
    assessments = [assess_evidence_item(claim, item, context) for item in evidence or []]
    useful = [item for item in assessments if item["relevance"] >= 0.18 and item["weight"] > 0.03]
    if not useful:
        return {
            "score": 25,
            "reasoning": "No sufficiently relevant and timely evidence was found for the exact claim.",
            "status": "Uncertain",
            "evidence_assessments": assessments,
        }

    support = sum(item["weight"] * max(0, item["stance"]) for item in useful)
    contradiction = sum(item["weight"] * max(0, -item["stance"]) for item in useful)
    neutral = sum(item["weight"] * (1 - abs(item["stance"])) for item in useful)
    denominator = support + contradiction + neutral * 0.8
    signed_ratio = (support - contradiction) / max(0.001, denominator)
    score = round(max(5, min(95, 50 + signed_ratio * 45)))

    reliable_support = [
        item for item in useful
        if item["stance"] >= 0.35 and item["trust"] >= 0.68 and item["freshness"] >= 0.5
    ]
    reliable_contradiction = [
        item for item in useful
        if item["stance"] <= -0.35 and item["trust"] >= 0.68 and item["freshness"] >= 0.5
    ]
    if reliable_support and reliable_contradiction:
        status = "Disputed"
    elif score >= 70 and (len(reliable_support) >= 2 or any(i["trust"] >= 0.95 for i in reliable_support)):
        status = "Verified"
    elif reliable_contradiction and not reliable_support:
        status = "False"
        score = min(score, 25)
    else:
        status = "Uncertain"

    newest = max((item["freshness"] for item in useful), default=0)
    reasoning = (
        f"Assessed {len(useful)} relevant source(s): {len(reliable_support)} reliable support, "
        f"{len(reliable_contradiction)} reliable contradiction. "
        f"Newest-evidence freshness was {round(newest * 100)}%. "
    )
    if status == "Verified":
        reasoning += "Recent, reliable evidence supports the exact claim."
    elif status == "False":
        reasoning += "Recent, reliable evidence contradicts a concrete claim detail."
    elif status == "Disputed":
        reasoning += "Recent reliable sources materially disagree."
    else:
        reasoning += "The evidence is not specific or strong enough for a definitive verdict."
    return {
        "score": score,
        "reasoning": reasoning,
        "status": status,
        "evidence_assessments": assessments,
    }

def judge_fallback(claim, evidence):
    """Deterministic evidence-based scorer used when the AI judge is unavailable."""
    claim_lower = claim.lower()

    sports_result = judge_sports_result(claim, evidence)
    if sports_result:
        return sports_result

    false_indicators = ["flat earth", "vaccine autism", "moon landing fake", "5g covid", "chemtrails"]
    true_indicators = ["earth revolves", "climate change", "evolution", "gravity", "round earth"]

    if any(indicator in claim_lower for indicator in false_indicators):
        return {
            "score": 12,
            "reasoning": "The claim conflicts with well-established scientific evidence.",
            "status": "False"
        }
    if any(indicator in claim_lower for indicator in true_indicators):
        return {
            "score": 90,
            "reasoning": "The claim is consistent with well-established scientific evidence.",
            "status": "Verified"
        }

    return judge_evidence_deterministically(claim, evidence)

def build_verdict_sources(claim, evidence, verdict):
    """Expose the judge's source-level reasoning to the UI."""
    assessments = verdict.get("evidence_assessments") or []
    by_url = {item.get("url", ""): item for item in assessments if item.get("url")}
    sources = []
    # evidence is already trimmed by select_evidence; show exactly what was used
    for item in evidence:
        assessment = by_url.get(item.get("url", ""), {})
        sources.append({
            "title": item.get("title", "Source"),
            "url": item.get("url", ""),
            "excerpt": make_excerpt(item.get("content", ""), claim),
            "source_type": item.get("source_type", "general"),
            "published": item.get("published", ""),
            "publisher": item.get("publisher", ""),
            "provider": item.get("provider", ""),
            "stance": assessment.get("stance"),
            "relevance": assessment.get("relevance", item.get("relevance_score")),
            "freshness": assessment.get("freshness", item.get("freshness_score")),
            "trust": assessment.get("trust"),
            "support_reasons": assessment.get("support_reasons", []),
            "contradiction_reasons": assessment.get("contradiction_reasons", []),
        })
    return sources

def build_search_results(search_results):
    return [
        {
            "title": item.get("title", "Source"),
            "url": item.get("url", ""),
            "snippet": item.get("snippet", ""),
            "source_type": item.get("source_type", "general"),
            "published": item.get("published", ""),
            "publisher": item.get("publisher", ""),
            "provider": item.get("provider", ""),
            "relevance": item.get("relevance_score"),
            "freshness": item.get("freshness_score"),
            "trust": item.get("trust_score"),
            "rank_score": item.get("rank_score"),
        }
        for item in search_results[:12]
    ]

# ========== CRISIS TRIAGE LAYER (Phase 1 + 2) ==========
# Veritas is NOT an absolute-truth oracle. This layer separates confirmed facts,
# unverified claims, contradictions, official confirmation, and urgent risk under
# uncertainty. The backend is the single source of truth for the verdict; the
# frontend only displays it.

TRIAGE_STATUSES = [
    "Verified", "Likely true", "Unconfirmed", "Conflicting reports",
    "Likely false", "False", "Urgent but unverified",
]

# Map the richer triage status back to the legacy 4-value status so existing
# UI paths (ring colour etc.) keep working.
def legacy_status_from_triage(triage_status):
    return {
        "Verified": "Verified",
        "Likely true": "Verified",
        "Unconfirmed": "Uncertain",
        "Conflicting reports": "Disputed",
        "Likely false": "False",
        "False": "False",
        "Urgent but unverified": "Uncertain",
    }.get(triage_status, "Uncertain")

OFFICIAL_TYPES = {"government", "education", "research"}
MEDIA_TYPES = {"news", "news_reputable", "fact_check"}
SOCIAL_TYPES = {"social", "anonymous"}

def official_signal(evidence):
    """Deterministically derive official/media/social presence from source types."""
    evidence = evidence or []
    official = [e for e in evidence if e.get("source_type") in OFFICIAL_TYPES]
    media = [e for e in evidence if e.get("source_type") in MEDIA_TYPES]
    social = [e for e in evidence if e.get("source_type") in SOCIAL_TYPES]
    return {
        "official_sources_found": [
            {"title": e.get("title", ""), "url": e.get("url", ""),
             "source_type": e.get("source_type", "")}
            for e in official
        ][:6],
        "media_only": bool(media) and not official and not social,
        "social_only": bool(social) and not official and not media,
        "_has_official": bool(official),
    }

def deterministic_assessments(claim, evidence):
    """Per-source explainability (stance/relevance/trust), computed without the LLM."""
    ctx = extract_context_fallback(claim)
    return [assess_evidence_item(claim, item, ctx) for item in evidence or []]

def select_evidence(claim, context, evidence, max_keep=10, min_keep=3, rel_floor=0.22):
    """Pick the sources that are actually relevant to THIS claim, so the evidence
    set (and the displayed source count) varies by case instead of always being a
    fixed top-N. Authoritative sources clear a lower relevance bar; if too few
    pass, fall back to the strongest available so we never show nothing."""
    if not evidence:
        return []
    ctx = {
        "location": (context or {}).get("location"),
        "claim_type": (context or {}).get("claimType") or (context or {}).get("claim_type"),
    }
    scored = []
    for item in evidence:
        rel = assess_evidence_item(claim, item, ctx).get("relevance", 0) or 0
        official = item.get("source_type") in OFFICIAL_TYPES
        scored.append((rel, official, item))
    kept = [t for t in scored if t[0] >= rel_floor or (t[1] and t[0] >= 0.10)]
    kept.sort(key=lambda t: (t[1], t[0]), reverse=True)
    if len(kept) < min_keep:
        kept = sorted(scored, key=lambda t: (t[1], t[0]), reverse=True)[:min_keep]
    selected = [item for (_, _, item) in kept[:max_keep]]
    print(f"🔎 Selected {len(selected)}/{len(evidence)} relevant sources", flush=True)
    return selected

def triage_analysis(claim, context, evidence):
    """Crisis-triage call, traced to Arize/Phoenix with the verdict as signals."""
    with trace_span("triage_analysis", **{
        "input.claim": (claim or "")[:500],
        "input.language": (context or {}).get("language") or "en",
        "input.evidence_count": len(evidence or []),
    }) as span:
        result = _triage_analysis_impl(claim, context, evidence)
        if result:
            _span_set(span,
                      **{"triage.status": result.get("triage_status"),
                         "triage.evidence_confidence": result.get("evidence_confidence"),
                         "triage.official_confirmation": result.get("official_confirmation"),
                         "triage.subclaims": len(result.get("subclaims") or [])})
        return result

def _triage_analysis_impl(claim, context, evidence):
    """Single structured Claude call producing the full crisis-triage verdict.
    Returns None if unavailable so the caller can fall back to judge_claim()."""
    if not ANTHROPIC_API_KEY:
        return None
    context = context or {}

    ev_text = ""
    for i, e in enumerate(evidence or []):
        content = (e.get("content") or "")
        if content and len(content) > 40:
            ev_text += (
                f"\n[{i+1}] {e.get('title', 'Untitled')} "
                f"| type={e.get('source_type', '?')} "
                f"| published={e.get('published') or '?'}\n{content[:1200]}\n"
            )
    if not ev_text:
        ev_text = "No usable evidence was found yet."

    language = (context.get('language') or 'en').strip() or 'en'
    ctx_text = (
        f"location={context.get('location') or 'unknown'}; "
        f"incident_time={context.get('incidentTime') or context.get('datetime') or 'unknown'}; "
        f"incident_type={context.get('claimType') or 'unknown'}; "
        f"language={language}"
    )

    prompt = f"""You are Veritas, a CRISIS INFORMATION TRIAGE system. You do NOT decide absolute truth. Using ONLY the supplied evidence, you separate confirmed facts, unverified claims, contradictions, official confirmation, and urgent risk under uncertainty.

CLAIM: "{claim}"
USER CONTEXT: {ctx_text}

EVIDENCE (each tagged with source type and publish time):
{ev_text}

Rules:
- USE the context: reward evidence whose location, time and incident type MATCH the claim; discount evidence that is vague, off-location, or stale.
- "No official source found" is NOT the same as false. Early in a disaster the official confirmation may not exist yet.
- Use "Urgent but unverified" when the claim involves public safety, is plausible, but lacks official confirmation — do NOT dismiss it.
- Decompose the claim into sub-claims (event occurred, location, time, magnitude/severity, casualties, infrastructure, etc.) and judge each separately.
- A search page or a source merely repeating the query is NOT confirmation.
- evidence_confidence is the STRENGTH OF CURRENT EVIDENCE, not a probability of truth.

Scoring guide for evidence_confidence (strength of current evidence):
- 80-100: multiple independent, reliable, on-point sources confirm it
- 50-79: some credible support but gaps remain
- 20-49: weak / mostly background / single weak source
- 0-19: no usable evidence either way

Return ONLY valid JSON in exactly this shape (replace each <...> with a real value):
{{
  "evidence_confidence": <integer 0-100 per the guide above>,
  "triage_status": "<one of: Verified | Likely true | Unconfirmed | Conflicting reports | Likely false | False | Urgent but unverified>",
  "summary": "<2-3 sentences>",
  "subclaims": [{{"claim": "<sub-claim>", "status": "<Verified|Likely true|Unconfirmed|Conflicting reports|Likely false|False>", "confidence": <integer 0-100>, "evidence": "<short note>"}}],
  "confirmed_facts": ["<facts the evidence actually confirms>"],
  "unverified_claims": ["<parts not yet confirmed>"],
  "contradictions": ["<conflicts between sources, if any>"],
  "official_confirmation": "<one of: confirmed | contradicted | not_found | unclear>",
  "official_silence_risk": "<one of: low | medium | high>",
  "recommendation": "<what the user should do; for urgent unverified claims advise NOT sharing as confirmed and monitoring official/local emergency channels>"
}}
Consistency: if triage_status is Verified or Likely true, evidence_confidence should be 60+. If there is no evidence at all, use a low evidence_confidence.

LANGUAGE: Write every human-readable text value — summary, recommendation, and each subclaim's claim/evidence, plus all confirmed_facts / unverified_claims / contradictions — in the language with ISO code "{language}" (the user's language). Keep the JSON keys and the enum values (triage_status, official_confirmation, official_silence_risk, subclaim status) EXACTLY as the English strings shown above — translate only the free text.
Output ONLY the JSON object, nothing else."""

    try:
        print(f"🧭 Running crisis-triage analysis ({TRIAGE_MODEL})...", flush=True)
        resp = claude.messages.create(
            model=TRIAGE_MODEL,
            max_tokens=1200,
            temperature=0.2,
            messages=[{"role": "user", "content": prompt}],
        )
        # Robustly pull the JSON from the text block (tolerates a thinking
        # block appearing first if adaptive thinking is ever enabled).
        text = "".join(b.text for b in resp.content if getattr(b, "type", None) == "text")
        data = parse_judge_json(text or resp.content[0].text)
    except Exception as e:
        print(f"❌ triage_analysis error: {type(e).__name__}: {e}", flush=True)
        return None

    sig = official_signal(evidence)

    ts = data.get("triage_status")
    if ts not in TRIAGE_STATUSES:
        ts = "Unconfirmed"

    oc = data.get("official_confirmation")
    if oc not in ("confirmed", "contradicted", "not_found", "unclear"):
        oc = "unclear"
    # cannot be "officially confirmed" if no official source is present
    if not sig["_has_official"] and oc == "confirmed":
        oc = "not_found"

    osr = data.get("official_silence_risk")
    if osr not in ("low", "medium", "high"):
        osr = "medium"

    try:
        conf = max(0, min(100, int(data.get("evidence_confidence", 50))))
    except (TypeError, ValueError):
        conf = 50

    def _strlist(key):
        return [str(x).strip() for x in (data.get(key) or []) if str(x).strip()][:8]

    subclaims = []
    for s in (data.get("subclaims") or []):
        if isinstance(s, dict) and s.get("claim"):
            try:
                sc_conf = max(0, min(100, int(s.get("confidence", 50))))
            except (TypeError, ValueError):
                sc_conf = 50
            subclaims.append({
                "claim": str(s.get("claim"))[:240],
                "status": str(s.get("status") or "Unconfirmed"),
                "confidence": sc_conf,
                "evidence": str(s.get("evidence") or "")[:240],
            })

    return {
        "evidence_confidence": conf,
        "triage_status": ts,
        "summary": str(data.get("summary") or ""),
        "subclaims": subclaims[:8],
        "confirmed_facts": _strlist("confirmed_facts"),
        "unverified_claims": _strlist("unverified_claims"),
        "contradictions": _strlist("contradictions"),
        "official_confirmation": oc,
        "official_silence_risk": osr,
        "official_sources_found": sig["official_sources_found"],
        "media_only": sig["media_only"],
        "social_only": sig["social_only"],
        "recommendation": str(data.get("recommendation") or ""),
        "disclaimer": "This is evidence confidence, not absolute truth.",
    }

def run_triage(claim, context, evidence):
    """Produce the full verdict: triage layer if available, else legacy judge."""
    triage = triage_analysis(claim, context, evidence)
    if triage:
        verdict = dict(triage)
        verdict["score"] = triage["evidence_confidence"]
        verdict["status"] = legacy_status_from_triage(triage["triage_status"])
        verdict["reasoning"] = triage.get("summary") or ""
        verdict["evidence_assessments"] = deterministic_assessments(claim, evidence)
        return verdict
    # fallback: legacy judge (no API key / error) — still expose triage fields
    verdict = judge_claim(claim, evidence)
    sig = official_signal(evidence)
    verdict.setdefault("evidence_confidence", verdict.get("score", 50))
    verdict.setdefault("triage_status", {
        "Verified": "Verified", "False": "False", "Disputed": "Conflicting reports",
    }.get(verdict.get("status"), "Unconfirmed"))
    verdict.setdefault("summary", verdict.get("reasoning", ""))
    verdict.setdefault("subclaims", [])
    verdict.setdefault("confirmed_facts", [])
    verdict.setdefault("unverified_claims", [])
    verdict.setdefault("contradictions", [])
    verdict.setdefault("official_confirmation", "not_found" if not sig["_has_official"] else "unclear")
    verdict.setdefault("official_silence_risk", "medium")
    verdict.setdefault("official_sources_found", sig["official_sources_found"])
    verdict.setdefault("media_only", sig["media_only"])
    verdict.setdefault("social_only", sig["social_only"])
    verdict.setdefault("recommendation", "")
    verdict.setdefault("disclaimer", "This is evidence confidence, not absolute truth.")
    return verdict

# ========== MULTI-CLAIM (split independent claims, verify each) ==========
def _maybe_multiclaim(text):
    """Cheap gate: only attempt a split when the text plausibly holds >1 claim,
    so single claims don't pay an extra LLM call. Counts sentence terminators
    followed by a space/end (so decimals like '5.8' aren't miscounted) — two or
    more sentences is the trigger."""
    t = (text or "").strip()
    if len(t) < 25:
        return False
    sentences = len(re.findall(r'[.。!?！？;](?:\s|$)', t)) + t.count("\n")
    return sentences >= 2

def split_claims(text, max_claims=5):
    """Split free text into independent, separately-checkable claims.
    Returns a list (length 1 means treat as a single claim)."""
    if not ANTHROPIC_API_KEY:
        return [text]
    prompt = (
        "Split the text into INDEPENDENT, separately fact-checkable claims. "
        "Two assertions about the SAME event/incident are ONE claim — only split "
        "claims that are about genuinely different topics or events. Rewrite each "
        "as a standalone sentence in the text's original language. If the text is "
        "really a single claim, return just that one.\n\n"
        f"TEXT: \"{text}\"\n\n"
        'Return ONLY JSON: {"claims": ["claim 1", "claim 2"]}'
    )
    try:
        resp = claude.messages.create(
            model=FAST_MODEL, max_tokens=400, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        data = parse_judge_json(resp.content[0].text)
        claims = [str(c).strip() for c in (data.get("claims") or []) if str(c).strip()]
    except Exception as e:
        print(f"⚠️ split_claims error: {e}", flush=True)
        return [text]
    # dedupe (case-insensitive), cap
    seen, out = set(), []
    for c in claims:
        k = c.lower()
        if k not in seen:
            seen.add(k); out.append(c)
    return out[:max_claims] or [text]

def build_full_verdict(claim, context):
    """Run the whole pipeline for one claim (no SSE) and return its verdict.
    Used by the multi-claim parallel path; safe to run in a worker thread."""
    incident_type = (context or {}).get('claimType')
    search_results = search_web(claim, incident_type, context)
    evidence = extract_evidence(search_results)
    evidence = enrich_evidence_with_snippets(search_results, evidence)
    evidence = select_evidence(claim, context, evidence)
    verdict = run_triage(claim, context, evidence)
    verdict["claim"] = claim
    verdict["sources"] = build_verdict_sources(claim, evidence, verdict)
    verdict["search_results"] = build_search_results(search_results)
    return verdict

# ========== URL MODE (Phase 2): verify the article/post, not the URL string ==========
def looks_like_url(text):
    return bool(re.match(r'^https?://', (text or "").strip(), re.I))

def is_safe_url(url):
    """Basic SSRF guard: http/https only, block localhost / private / reserved IPs.
    TODO(Phase 4): also re-validate after redirects, cap response size, content-type."""
    try:
        p = urlparse(url)
    except Exception:
        return False
    if p.scheme not in ("http", "https"):
        return False
    host = (p.hostname or "").lower()
    if not host or host in ("localhost",):
        return False
    try:
        for info in socket.getaddrinfo(host, None):
            ip = ipaddress.ip_address(info[4][0])
            if (ip.is_private or ip.is_loopback or ip.is_link_local
                    or ip.is_reserved or ip.is_multicast or ip.is_unspecified):
                return False
    except Exception:
        return False
    return True

def _looks_like_garbage(text):
    """True if text is mostly non-readable (undecoded/binary/mojibake)."""
    t = text or ""
    if len(t) < 20:
        return True
    bad = sum(1 for c in t if c == "�" or (ord(c) < 32 and c not in "\t\n\r"))
    # also count chars outside common printable/letter ranges as a rough signal
    return (bad / max(1, len(t))) > 0.10

# phrases that mean the LLM could not derive a claim — never use these as the claim
_NO_CLAIM_MARKERS = (
    "cannot extract", "can't extract", "unable to extract", "no claim",
    "not readable", "corrupted", "encoded", "binary data", "no coherent",
    "抽出できません", "判読できません", "読み取れません",
)

def extract_main_claim_from_text(text, url=""):
    """Use Claude to pull the single main verifiable claim out of page/post text.
    Returns None when the text is unreadable or no claim can be derived."""
    if _looks_like_garbage(text):
        print("⚠️ extract_main_claim: content looks unreadable; skipping", flush=True)
        return None
    if not ANTHROPIC_API_KEY:
        for sent in re.split(r'(?<=[.!?])\s+', re.sub(r'\s+', ' ', text or '')):
            if len(sent) > 40:
                return sent[:240]
        return None
    snippet = (text or "")[:3000]
    prompt = (
        "From the following article or social post, extract the single MAIN factual "
        "claim being asserted, as one concise sentence suitable for fact-checking. "
        "If several, choose the most important verifiable one. If the content is "
        "unreadable or contains no factual claim, reply with exactly NO_CLAIM.\n\n"
        f"URL: {url}\nCONTENT:\n{snippet}\n\nReturn ONLY the claim sentence (or NO_CLAIM)."
    )
    try:
        resp = claude.messages.create(
            model=FAST_MODEL, max_tokens=120, temperature=0,
            messages=[{"role": "user", "content": prompt}],
        )
        out = resp.content[0].text.strip().strip('"').strip()
    except Exception as e:
        print(f"⚠️ extract_main_claim_from_text error: {e}", flush=True)
        return None
    low = out.lower()
    if not out or "no_claim" in low or any(m in low for m in _NO_CLAIM_MARKERS):
        return None
    return out

def normalize_source_url(url):
    """Rewrite known JS-rendered mirrors to a static-HTML equivalent so the
    extractor can actually read the body. (itest.5ch.io renders posts via JS;
    the canonical {server}.5ch.net read.cgi serves them server-side.)"""
    m = re.match(r'https?://itest\.5ch\.io/([^/]+)/test/read\.cgi/(\w+)/(\d+)', url or "")
    if m:
        server, board, tid = m.groups()
        return f"https://{server}.5ch.net/test/read.cgi/{board}/{tid}/"
    return url

def resolve_url_claim(url):
    """Fetch a pasted URL, extract its body, and derive the main claim to verify.
    Returns an original_source dict (never raises)."""
    fetch_url = normalize_source_url(url)  # display the pasted URL, fetch the readable one
    if not is_safe_url(fetch_url):
        return {"url": url, "title": url, "main_claim": None, "excerpt": "",
                "error": "blocked_or_invalid_url"}
    content, title = "", url
    try:
        r = requests.post(EXTRACTOR_AGENT_URL, json={"urls": [fetch_url], "method": "basic"}, timeout=30)
        if r.status_code == 200:
            ev = (r.json().get("evidence") or [{}])
            ev = ev[0] if ev else {}
            content = ev.get("content", "") or ""
            title = ev.get("title") or url
    except Exception as e:
        print(f"⚠️ resolve_url_claim fetch error: {e}", flush=True)
    if not content or len(content) < 80:
        return {"url": url, "title": title, "main_claim": None, "excerpt": content[:300],
                "error": "no_readable_content"}
    main_claim = extract_main_claim_from_text(content, url)
    if not main_claim:
        return {"url": url, "title": title, "main_claim": None, "excerpt": content[:300],
                "error": "no_claim_found"}
    return {"url": url, "title": title, "main_claim": main_claim, "excerpt": content[:300]}

# ========== API ENDPOINTS ==========

@app.route('/evaluate', methods=['POST'])
def evaluate():
    """Full evaluation: Search → Extract → Judge"""
    try:
        data = request.json
        claim = data.get('claim', '').strip()
        context = data.get('context') or {}
        incident_type = context.get('claimType')

        if not claim or len(claim) < 3:
            return jsonify({"error": "Please enter a valid claim (at least 3 characters)"}), 400

        print(f"\n{'='*50}")
        print(f"📝 Evaluating: {claim}")
        print(f"{'='*50}")

        # STEP 0: URL mode — verify the linked article/post, not the URL string
        original_source = None
        if context.get('inputType') == 'url' or looks_like_url(claim):
            url = (context.get('url') or claim).strip()
            print(f"🔗 URL mode: resolving {url}")
            original_source = resolve_url_claim(url)
            if original_source.get('main_claim'):
                claim = original_source['main_claim']
                print(f"🔗 Extracted claim: {claim}")
            else:
                # Couldn't read a verifiable claim — don't fact-check the URL string
                return jsonify({
                    "error": "url_unreadable",
                    "message": "Could not read a verifiable claim from that link "
                               "(the page may be a discussion thread, paywalled, "
                               "or render its text with JavaScript).",
                    "original_source": original_source,
                }), 200

        # STEP 1: Search the web (routed by incident type + user context)
        search_results = search_web(claim, incident_type, context)

        # STEP 2: Extract evidence
        evidence = extract_evidence(search_results)

        # STEP 2.5: Make sure search snippets (USGS / news) reach the judge,
        # then select the genuinely relevant ones (count varies by case).
        evidence = enrich_evidence_with_snippets(search_results, evidence)
        evidence = select_evidence(claim, context, evidence)

        # STEP 3: Crisis triage (context-aware, structured) — backend is the
        # single source of truth for the verdict.
        verdict = run_triage(claim, context, evidence)
        verdict["claim"] = claim

        # Add source information (show all evidence the judge actually saw,
        # so the confirming news sources appear — not just the first few)
        verdict["sources"] = build_verdict_sources(claim, evidence, verdict)

        # Add search results for transparency
        verdict["search_results"] = build_search_results(search_results)

        # URL mode: keep the pasted source separate from the verification sources
        if original_source:
            verdict["original_source"] = original_source
            verdict["verification_sources"] = verdict["sources"]
            verdict["resolved_claim"] = claim

        print(f"✅ Score: {verdict['score']}/100 - {verdict['status']}")
        print(f"📚 Sources: {len(verdict['sources'])}")
        print(f"{'='*50}\n")

        return jsonify(verdict)

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/extract-context', methods=['POST'])
def extract_context_endpoint():
    """Auto-detect location / date-time / claim type from the claim text."""
    data = request.json or {}
    claim = (data.get('claim', '') or '').strip()
    if not claim:
        return jsonify({"location": None, "datetime": None, "claim_type": "other"})
    return jsonify(extract_context(claim))

@app.route('/evaluate/stream', methods=['POST'])
def evaluate_stream():
    """Same pipeline as /evaluate but streams real per-stage progress via SSE."""
    data = request.json or {}
    claim = (data.get('claim', '') or '').strip()
    context = data.get('context') or {}
    incident_type = context.get('claimType')

    def sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        nonlocal claim  # URL mode reassigns claim below; without this it's a local
        if not claim or len(claim) < 3:
            yield sse({"stage": "error", "message": "Please enter a valid claim (at least 3 characters)"})
            return
        try:
            print(f"\n{'='*50}\n📝 [stream] Evaluating: {claim}\n{'='*50}")

            # STEP 0: URL mode — verify the linked article/post, not the URL string
            original_source = None
            if context.get('inputType') == 'url' or looks_like_url(claim):
                url = (context.get('url') or claim).strip()
                yield sse({"stage": "resolve", "status": "active"})
                original_source = resolve_url_claim(url)
                if original_source.get('main_claim'):
                    claim = original_source['main_claim']
                    yield sse({"stage": "resolve", "status": "done", "claim": claim})
                else:
                    # Couldn't read a verifiable claim — stop with a clear message
                    yield sse({"stage": "resolve", "status": "done", "claim": None})
                    yield sse({"stage": "error",
                               "message": "Could not read a verifiable claim from that link "
                                          "(the page may be a discussion thread, paywalled, "
                                          "or render its text with JavaScript).",
                               "original_source": original_source})
                    return

            # STEP 0.5: multi-claim — split independent claims and verify each in
            # parallel. Only for text mode (a resolved URL is treated as one claim).
            if original_source is None and _maybe_multiclaim(claim):
                claims = split_claims(claim)
                if len(claims) > 1:
                    print(f"🧩 Multi-claim: {len(claims)} claims detected")
                    yield sse({"stage": "multi", "claims": claims})
                    with ThreadPoolExecutor(max_workers=min(5, len(claims))) as ex:
                        futures = {ex.submit(build_full_verdict, c, context): (i, c)
                                   for i, c in enumerate(claims)}
                        done = 0
                        for fut in as_completed(futures):
                            i, c = futures[fut]
                            try:
                                v = fut.result()
                            except Exception as e:
                                v = {"claim": c, "error": str(e), "triage_status": "Unconfirmed",
                                     "evidence_confidence": 0, "summary": "Verification failed."}
                            done += 1
                            yield sse({"stage": "claim_result", "index": i,
                                       "total": len(claims), "done": done, "verdict": v})
                    yield sse({"stage": "done"})
                    return

            # STEP 1: search (routed by incident type + user context)
            yield sse({"stage": "search", "status": "active"})
            search_results = search_web(claim, incident_type, context)
            yield sse({"stage": "search", "status": "done", "count": len(search_results)})

            # STEP 2: extract (+ snippet enrichment)
            yield sse({"stage": "extract", "status": "active"})
            evidence = extract_evidence(search_results)
            evidence = enrich_evidence_with_snippets(search_results, evidence)
            evidence = select_evidence(claim, context, evidence)
            yield sse({"stage": "extract", "status": "done", "count": len(evidence)})

            # STEP 3: crisis triage (context-aware, structured)
            yield sse({"stage": "judge", "status": "active"})
            verdict = run_triage(claim, context, evidence)
            verdict["claim"] = claim
            verdict["sources"] = build_verdict_sources(claim, evidence, verdict)
            verdict["search_results"] = build_search_results(search_results)
            if original_source:
                verdict["original_source"] = original_source
                verdict["verification_sources"] = verdict["sources"]
                verdict["resolved_claim"] = claim
            yield sse({"stage": "judge", "status": "done"})

            print(f"✅ [stream] Score: {verdict['score']}/100 - {verdict['status']}")
            yield sse({"stage": "result", "verdict": verdict})

        except Exception as e:
            print(f"❌ [stream] Error: {e}")
            yield sse({"stage": "error", "message": str(e)})

    return Response(generate(), mimetype='text/event-stream',
                    headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

@app.route('/health', methods=['GET'])
def health():
    agents_status = {}

    try:
        r = requests.get(SEARCHER_AGENT_URL.rsplit("/", 1)[0] + "/health", timeout=2)
        agents_status["searcher"] = "online" if r.status_code == 200 else "offline"
    except:
        agents_status["searcher"] = "offline"

    try:
        r = requests.get(EXTRACTOR_AGENT_URL.rsplit("/", 1)[0] + "/health", timeout=2)
        agents_status["extractor"] = "online" if r.status_code == 200 else "offline"
    except:
        agents_status["extractor"] = "offline"

    return jsonify({
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "status": "alive",
        "agents": agents_status,
        "message": "Ready to evaluate claims!"
    })

@app.route('/info', methods=['GET'])
def info():
    return jsonify({
        "name": AGENT_NAME,
        "version": AGENT_VERSION,
        "description": "AI fact-checking system with 3 agents",
        "agents": {
            "searcher": SEARCHER_AGENT_URL,
            "extractor": EXTRACTOR_AGENT_URL,
            "judge": "http://localhost:5003"
        },
        "endpoints": {
            "evaluate": "POST /evaluate - Full evaluation",
            "health": "GET /health - Check status"
        }
    })

if __name__ == '__main__':
    print("\n" + "="*50)
    print(f"🧠 {AGENT_NAME} v{AGENT_VERSION}")
    print("="*50)
    print("📍 Running on: http://localhost:5003")
    print("🔗 Endpoint: http://localhost:5003/evaluate")
    print("📊 Health: http://localhost:5003/health")
    print("="*50)
    print("📊 Traces being sent to Arize Phoenix!")
    print("⚡ Ready to fact-check!\n")

    app.run(host='0.0.0.0', port=5003, debug=False, use_reloader=False)
