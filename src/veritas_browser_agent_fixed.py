from flask import Flask, request, jsonify, Response
from flask_cors import CORS
import os
import re
import json
import requests
import anthropic

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
        print("⚠️ Searcher Agent not running! Using mock data.")
        return mock_search_results(claim)
    except Exception as e:
        print(f"❌ Search error: {e}")
        return mock_search_results(claim)

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
                evidence_text += f"\nSource {i+1}: {e.get('title', 'Untitled')}\nContent: {content[:1500]}\n"
    else:
        evidence_text = "No specific evidence found. Use your general knowledge."

    # Build the prompt for Claude
    prompt = f"""You are Veritas, an AI truth-judge. Evaluate this claim based on the provided evidence.

CLAIM: "{claim}"

EVIDENCE:
{evidence_text}

Based on the evidence, provide:
1. A SCORE from 0-100
   - 0-30 = False (claim is clearly false)
   - 31-69 = Disputed/Uncertain (claim is contested or unclear)
   - 70-100 = Verified (claim is clearly true)

2. REASONING (2-3 sentences explaining your score)

3. STATUS (choose exactly one: Verified, False, Disputed, or Uncertain)

Return ONLY valid JSON in this format:
{{"score": 75, "reasoning": "The evidence supports this claim because...", "status": "Verified"}}

Make sure your response is ONLY the JSON object, nothing else.
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
            "status": status
        }

    except Exception as e:
        print(f"❌ Claude error: {type(e).__name__}: {e}", flush=True)
        return judge_fallback(claim, evidence)

# ========== FALLBACK JUDGE ==========
def judge_fallback(claim, evidence):
    """Deterministic evidence-based scorer used when the AI judge is unavailable."""
    claim_lower = claim.lower()

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

    stopwords = {
        "about", "after", "again", "against", "also", "and", "are", "as", "at",
        "be", "been", "before", "but", "by", "for", "from", "had", "has", "have",
        "in", "into", "is", "it", "its", "near", "of", "on", "or", "some",
        "that", "the", "their", "there", "this", "to", "toward", "was", "were",
        "with",
    }
    claim_tokens = {
        w for w in re.findall(r"[a-z0-9]+", claim_lower)
        if len(w) >= 3 and w not in stopwords
    }
    trust = {
        "government": 1.0, "education": 0.95, "research": 0.9,
        "fact_check": 0.9, "news_reputable": 0.85, "encyclopedia": 0.7,
        "news": 0.68, "general": 0.45, "unknown": 0.35,
    }
    contradiction_terms = (
        "false", "fake", "hoax", "debunk", "no evidence", "did not",
        "has not", "denied", "incorrect", "misleading",
    )

    assessed = []
    for item in evidence or []:
        text = " ".join((
            item.get("title", "") or "",
            item.get("content", "") or "",
        )).lower()
        text_tokens = set(re.findall(r"[a-z0-9]+", text))
        overlap = len(claim_tokens & text_tokens)
        relevance = overlap / max(1, min(len(claim_tokens), 10))
        source_trust = trust.get(item.get("source_type", "unknown"), 0.4)
        contradicts = relevance >= 0.2 and any(term in text for term in contradiction_terms)
        assessed.append((relevance, source_trust, contradicts))

    relevant = [a for a in assessed if a[0] >= 0.15]
    supporting = [a for a in relevant if not a[2]]
    contradicting = [a for a in relevant if a[2]]

    if not relevant:
        return {
            "score": 35,
            "reasoning": "Sources were found, but they do not closely match the specific claim. More targeted evidence is needed.",
            "status": "Uncertain",
        }

    support_mass = sum(rel * src_trust for rel, src_trust, _ in supporting)
    contradict_mass = sum(rel * src_trust for rel, src_trust, _ in contradicting)
    avg_relevance = sum(rel for rel, _, _ in relevant) / len(relevant)
    reliable_count = sum(1 for rel, src_trust, _ in relevant if src_trust >= 0.68 and rel >= 0.2)

    score = 40
    score += min(32, support_mass * 10)
    score += min(16, reliable_count * 4)
    score += min(12, avg_relevance * 18)
    score -= min(35, contradict_mass * 14)
    score = max(5, min(95, round(score)))

    if contradict_mass > support_mass and score <= 35:
        status = "False"
    elif score >= 70 and reliable_count >= 2:
        status = "Verified"
    elif contradicting and supporting:
        status = "Disputed"
    else:
        status = "Uncertain"

    reasoning = (
        f"{len(relevant)} relevant source(s) were assessed, including "
        f"{reliable_count} higher-reliability source(s). "
    )
    if status == "Verified":
        reasoning += "The available evidence substantially supports the claim."
    elif status == "False":
        reasoning += "The stronger available evidence contradicts the claim."
    elif status == "Disputed":
        reasoning += "The sources contain meaningful conflicting signals."
    else:
        reasoning += "The evidence is related but not strong or specific enough for verification."

    return {"score": score, "reasoning": reasoning, "status": status}

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
        verdict["sources"] = [
            {"title": e.get('title', 'Source'), "url": e.get('url', ''),
             "excerpt": make_excerpt(e.get('content', ''), claim),
             "source_type": e.get("source_type", "general")}
            for e in evidence[:12]
        ]

        # Add search results for transparency
        verdict["search_results"] = [
            {"title": s.get('title', 'Source'), "url": s.get('url', ''),
             "snippet": s.get("snippet", ""),
             "source_type": s.get("source_type", "general")}
            for s in search_results[:12]
        ]

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
            verdict["sources"] = [
                {"title": e.get('title', 'Source'), "url": e.get('url', ''),
                 "excerpt": make_excerpt(e.get('content', ''), claim),
                 "source_type": e.get("source_type", "general")}
                for e in evidence[:12]
            ]
            verdict["search_results"] = [
                {"title": s.get('title', 'Source'), "url": s.get('url', ''),
                 "snippet": s.get("snippet", ""),
                 "source_type": s.get("source_type", "general")}
                for s in search_results[:12]
            ]
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
