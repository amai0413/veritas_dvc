from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import os
import re
import json
import requests
import anthropic
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime

# ========== ARIZE PHOENIX TRACING (opt-in) ==========
# The hosted Phoenix endpoint has been returning 500s and retrying, which adds
# latency + log noise to every request. Tracing is now OFF unless you opt in
# with ENABLE_PHOENIX=1, so the judge stays fast and quiet by default.
ENABLE_PHOENIX = os.environ.get("ENABLE_PHOENIX", "") == "1"

app = Flask(__name__)
CORS(app)

ANTHROPIC_API_KEY = os.environ.get("ANTHROPIC_API_KEY", "")

# ========== ARIZE PHOENIX SETUP ==========
if ENABLE_PHOENIX:
    print("🔍 Setting up Arize Phoenix tracing...")
    os.environ.setdefault("PHOENIX_PROJECT_NAME", "veritas-judge-agent")
    try:
        from phoenix.otel import register
        from openinference.instrumentation.anthropic import AnthropicInstrumentor
        tracer_provider = register(project_name="veritas-judge-agent", auto_instrument=True)
        AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)
        print("✅ Arize Phoenix tracing initialized!")
    except Exception as e:
        print(f"⚠️ Phoenix initialization warning: {e}")
        print("⚠️ Continuing without tracing...")
else:
    print("ℹ️ Phoenix tracing disabled (set ENABLE_PHOENIX=1 to enable).")

# ========== AGENT ENDPOINTS ==========
SEARCHER_AGENT_URL = os.environ.get(
    "SEARCHER_AGENT_URL", "http://127.0.0.1:5001/search"
)
EXTRACTOR_AGENT_URL = os.environ.get(
    "EXTRACTOR_AGENT_URL", "http://127.0.0.1:5002/extract"
)

# ========== INITIALIZE CLAUDE ==========
claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

# ========== AGENT IDENTITY ==========
AGENT_NAME = "Veritas Judge Agent"
AGENT_VERSION = "2.0"

# ========== SEARCH FUNCTION ==========
def search_web(claim, incident_type=None):
    """Call the Searcher Agent to find sources (routed by incident type)"""
    try:
        print(f"🔍 Calling Searcher Agent for: {claim} (type: {incident_type or 'auto'})")

        response = requests.post(
            SEARCHER_AGENT_URL,
            json={"claim": claim, "search_engine": "all", "incident_type": incident_type},
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
    for s in search_results[:12]:
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

    return {"location": location, "datetime": incident_time, "claim_type": claim_type}

def extract_context(claim):
    """Use Claude to read the incident location, date/time, and type from the claim text."""
    fallback = extract_context_fallback(claim)
    if not ANTHROPIC_API_KEY:
        return fallback

    prompt = f"""Extract structured fields from this crisis/incident claim. Return ONLY a JSON object.

CLAIM: "{claim}"

Return exactly this shape:
{{"location": "<the place mentioned, e.g. 'Crete, Greece'>", "datetime": "<when it happened or was reported, copied as written, e.g. '2 hours ago' or 'June 19 2026'>", "claim_type": "<one incident type from the list below>"}}

claim_type must be exactly one of:
- earthquake, wildfire, flood_storm, volcano, other_disaster
- shooting, terrorism, violent_crime, conflict, protest
- rescue_request, shelter_status, infrastructure, donation_aid
- misinformation, other

Rules:
- If the location is not stated, set location to null.
- If no date/time is stated, set datetime to null.
- Choose the single best claim_type; use "other" if none clearly fits.
- Output ONLY the JSON object, nothing else."""
    try:
        resp = claude.messages.create(
            model="claude-haiku-4-5-20251001",
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
    return {
        "location": clean(data.get("location")) or fallback["location"],
        "datetime": clean(data.get("datetime")) or fallback["datetime"],
        "claim_type": ctype if ctype != "other" else fallback["claim_type"],
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
            model="claude-haiku-4-5-20251001",  # ← CORRECT MODEL NAME
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
    for item in evidence[:12]:
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

# ========== API ENDPOINTS ==========

@app.route('/evaluate', methods=['POST'])
def evaluate():
    """Full evaluation: Search → Extract → Judge"""
    try:
        data = request.json
        claim = data.get('claim', '').strip()
        incident_type = (data.get('context') or {}).get('claimType')

        if not claim or len(claim) < 3:
            return jsonify({"error": "Please enter a valid claim (at least 3 characters)"}), 400

        print(f"\n{'='*50}")
        print(f"📝 Evaluating: {claim}")
        print(f"{'='*50}")

        # STEP 1: Search the web (routed by incident type)
        search_results = search_web(claim, incident_type)

        # STEP 2: Extract evidence
        evidence = extract_evidence(search_results)

        # STEP 2.5: Make sure search snippets (USGS / news) reach the judge
        evidence = enrich_evidence_with_snippets(search_results, evidence)

        # STEP 3: Judge the claim
        verdict = judge_claim(claim, evidence)
        verdict["claim"] = claim

        # Add source information (show all evidence the judge actually saw,
        # so the confirming news sources appear — not just the first few)
        verdict["sources"] = build_verdict_sources(claim, evidence, verdict)

        # Add search results for transparency
        verdict["search_results"] = build_search_results(search_results)

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
    incident_type = (data.get('context') or {}).get('claimType')

    def sse(obj):
        return f"data: {json.dumps(obj)}\n\n"

    def generate():
        if not claim or len(claim) < 3:
            yield sse({"stage": "error", "message": "Please enter a valid claim (at least 3 characters)"})
            return
        try:
            print(f"\n{'='*50}\n📝 [stream] Evaluating: {claim}\n{'='*50}")

            # STEP 1: search (routed by incident type)
            yield sse({"stage": "search", "status": "active"})
            search_results = search_web(claim, incident_type)
            yield sse({"stage": "search", "status": "done", "count": len(search_results)})

            # STEP 2: extract (+ snippet enrichment)
            yield sse({"stage": "extract", "status": "active"})
            evidence = extract_evidence(search_results)
            evidence = enrich_evidence_with_snippets(search_results, evidence)
            yield sse({"stage": "extract", "status": "done", "count": len(evidence)})

            # STEP 3: judge
            yield sse({"stage": "judge", "status": "active"})
            verdict = judge_claim(claim, evidence)
            verdict["claim"] = claim
            verdict["sources"] = build_verdict_sources(claim, evidence, verdict)
            verdict["search_results"] = build_search_results(search_results)
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
