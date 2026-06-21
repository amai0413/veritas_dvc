"""
Veritas Searcher Agent v3.0
Standalone fact-check search agent.

Priority chain:
  1. Browserbase (real cloud browser — handles JS, anti-bot)
  2. DuckDuckGo Instant Answer API
  3. Wikipedia API
  4. Mock data (offline fallback)

Tracked via Arize Phoenix OTEL.
Run: python veritas_searcher_agent.py
API: POST http://localhost:5001/search  { "claim": "..." }
"""

import os
import re
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

load_dotenv()

# ========== ARIZE AX TRACING ==========
from arize.otel import register_otel, Endpoints
from opentelemetry import trace

try:
    tracer_provider = register_otel(
        endpoints=Endpoints.ARIZE,
        space_id=os.getenv("ARIZE_SPACE_ID"),
        api_key=os.getenv("ARIZE_API_KEY"),
        model_id="veritas-searcher-agent",
    )
    tracer = trace.get_tracer("veritas.searcher")
    print("✅ Arize AX tracing initialized")
except Exception as e:
    print(f"⚠️ Arize AX init warning: {e} — continuing without tracing")
    tracer = trace.get_tracer("veritas.searcher")

# ========== CONFIG ==========
BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID")

app = Flask(__name__)
CORS(app)

AGENT_NAME = "Veritas Searcher Agent"
AGENT_VERSION = "3.0"

# ========== TRUST SCORING ==========
_TRUST_SCORES = {
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

def _source_type(url: str) -> str:
    if not url:
        return "unknown"
    u = url.lower()
    if any(x in u for x in [".gov", "who.int", "nato.int"]):
        return "government"
    if any(x in u for x in [".edu", "ac.uk", "edu.au"]):
        return "education"
    if any(x in u for x in ["nature.com", "science.org", "pubmed", "ncbi", "plos.org"]):
        return "research"
    if any(x in u for x in ["factcheck.org", "politifact.com", "snopes.com"]):
        return "fact_check"
    if any(x in u for x in ["apnews.com", "reuters.com", "bbc.com", "npr.org", "nytimes.com", "washingtonpost.com"]):
        return "news_reputable"
    if "wikipedia.org" in u:
        return "encyclopedia"
    if any(x in u for x in ["news", "times", "post"]):
        return "news"
    return "general"

def _rank_and_dedupe(results: list) -> list:
    seen = set()
    unique = []
    for r in results:
        url = r.get("url", "")
        if url and url not in seen:
            seen.add(url)
            r["trust_score"] = _TRUST_SCORES.get(r.get("source_type", "unknown"), 30)
            unique.append(r)
    return sorted(unique, key=lambda x: x.get("trust_score", 0), reverse=True)

# ========== 1. BROWSERBASE SEARCH (primary) ==========
def search_browserbase(query: str) -> list:
    """Open a real Browserbase cloud browser and search DuckDuckGo."""
    if not BROWSERBASE_API_KEY or not BROWSERBASE_PROJECT_ID:
        print("⚠️ Browserbase keys not set — skipping browser search")
        return []
    try:
        from browserbase import Browserbase
        from playwright.sync_api import sync_playwright

        print(f"🌐 Browserbase: opening session for '{query}'")
        bb = Browserbase(api_key=BROWSERBASE_API_KEY)
        session = bb.sessions.create(project_id=BROWSERBASE_PROJECT_ID)
        cdp_url = f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}&sessionId={session.id}"

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            search_url = f"https://duckduckgo.com/?q={requests.utils.quote(query)}&kl=us-en"
            page.goto(search_url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2500)

            raw = page.evaluate("""
                () => {
                    const selectors = [
                        '[data-testid="result"]',
                        '.result',
                        '.web-result',
                        'article'
                    ];
                    let items = [];
                    for (const sel of selectors) {
                        items = Array.from(document.querySelectorAll(sel));
                        if (items.length > 0) break;
                    }
                    return items.slice(0, 10).map(el => ({
                        title: (el.querySelector('h2, h3, .result__title') || {}).innerText || '',
                        url:   (el.querySelector('a[href]') || {}).href || '',
                        snippet: (el.querySelector('[data-result="snippet"], .result__snippet, p') || {}).innerText || ''
                    })).filter(r => r.url && r.url.startsWith('http'));
                }
            """)
            browser.close()

        results = [
            {
                "title": r.get("title", "").strip(),
                "url": r["url"],
                "snippet": r.get("snippet", "").strip(),
                "source_type": _source_type(r["url"]),
            }
            for r in raw if r.get("url")
        ]
        print(f"✅ Browserbase found {len(results)} results")
        return results

    except ImportError:
        print("⚠️ playwright not installed. Run: pip install playwright && playwright install chromium")
        return []
    except Exception as e:
        print(f"⚠️ Browserbase search error: {e}")
        return []

# ========== 2. DUCKDUCKGO API FALLBACK ==========
def search_duckduckgo_api(query: str) -> list:
    try:
        resp = requests.get(
            "https://api.duckduckgo.com/",
            params={"q": query, "format": "json", "no_html": 1, "skip_disambig": 1},
            timeout=10,
        )
        data = resp.json()
        results = []
        if data.get("Abstract"):
            results.append({
                "title": data.get("Heading", "Abstract"),
                "url": data.get("AbstractURL", ""),
                "snippet": data.get("Abstract", ""),
                "source_type": _source_type(data.get("AbstractURL", "")),
            })
        for topic in data.get("RelatedTopics", [])[:6]:
            if isinstance(topic, dict) and "Text" in topic:
                results.append({
                    "title": topic.get("Text", "")[:80],
                    "url": topic.get("FirstURL", ""),
                    "snippet": topic.get("Text", ""),
                    "source_type": _source_type(topic.get("FirstURL", "")),
                })
        return [r for r in results if r.get("url")]
    except Exception as e:
        print(f"⚠️ DuckDuckGo API error: {e}")
        return []

# ========== 3. WIKIPEDIA API (always included) ==========
def search_wikipedia(query: str) -> list:
    try:
        resp = requests.get(
            "https://en.wikipedia.org/w/api.php",
            params={"action": "query", "list": "search", "srsearch": query,
                    "format": "json", "srlimit": 3},
            timeout=10,
        )
        items = resp.json().get("query", {}).get("search", [])
        results = []
        for item in items:
            title = item.get("title", "")
            results.append({
                "title": title,
                "url": f"https://en.wikipedia.org/wiki/{title.replace(' ', '_')}",
                "snippet": re.sub(r"<[^>]+>", "", item.get("snippet", "")),
                "source_type": "encyclopedia",
            })
        return results
    except Exception as e:
        print(f"⚠️ Wikipedia API error: {e}")
        return []

# ========== 4. MOCK FALLBACK ==========
def mock_search(claim: str) -> list:
    cl = claim.lower()
    if "vaccine" in cl and "autism" in cl:
        return [
            {"title": "Vaccines and Autism — Wikipedia", "url": "https://en.wikipedia.org/wiki/Vaccines_and_autism", "snippet": "Multiple studies show no link between vaccines and autism.", "source_type": "encyclopedia"},
            {"title": "CDC — Vaccines and Autism", "url": "https://www.cdc.gov/vaccinesafety/concerns/autism.html", "snippet": "CDC information on vaccines and autism.", "source_type": "government"},
        ]
    elif "earth" in cl and "sun" in cl:
        return [
            {"title": "Earth's Orbit — Wikipedia", "url": "https://en.wikipedia.org/wiki/Earth%27s_orbit", "snippet": "Earth orbits the Sun at 67,000 mph.", "source_type": "encyclopedia"},
            {"title": "NASA — Solar System", "url": "https://solarsystem.nasa.gov/planets/earth/overview/", "snippet": "NASA explains Earth's orbit around the Sun.", "source_type": "government"},
        ]
    elif "climate" in cl and "change" in cl:
        return [
            {"title": "Climate Change — Wikipedia", "url": "https://en.wikipedia.org/wiki/Climate_change", "snippet": "Scientific consensus confirms climate change.", "source_type": "encyclopedia"},
            {"title": "NASA Climate Evidence", "url": "https://climate.nasa.gov/evidence/", "snippet": "NASA's evidence for climate change.", "source_type": "government"},
        ]
    else:
        return [
            {"title": f"Wikipedia: {claim[:60]}", "url": f"https://en.wikipedia.org/wiki/{claim.replace(' ', '_')}", "snippet": f"Wikipedia article about {claim}", "source_type": "encyclopedia"},
            {"title": f"Google Scholar: {claim[:40]}", "url": f"https://scholar.google.com/scholar?q={requests.utils.quote(claim)}", "snippet": f"Academic papers about {claim}", "source_type": "research"},
        ]

# ========== MAIN SEARCH PIPELINE ==========
def search_web(claim: str) -> list:
    with tracer.start_as_current_span("searcher.search_web") as span:
        span.set_attribute("claim", claim)

        # 1. Browserbase real browser (best)
        results = search_browserbase(claim)

        # 2. DuckDuckGo API if browser failed
        if not results:
            print("⚠️ Falling back to DuckDuckGo API...")
            results = search_duckduckgo_api(claim)

        # 3. Wikipedia always merged in
        wiki = search_wikipedia(claim)
        results.extend(wiki)

        # 4. Mock if everything failed
        if not results:
            print("⚠️ All sources failed — using mock data")
            results = mock_search(claim)

        final = _rank_and_dedupe(results)[:8]
        trusted = sum(1 for r in final if r.get("source_type") in ("government", "education"))
        span.set_attribute("result_count", len(final))
        span.set_attribute("trusted_count", trusted)
        return final

# ========== FLASK ROUTES ==========

@app.route("/search", methods=["POST"])
def search():
    data = request.json or {}
    claim = data.get("claim", "").strip()
    if not claim:
        return jsonify({"error": "Provide a 'claim' field"}), 400

    print(f"\n{'='*50}\n🔍 Searching: {claim}\n{'='*50}")
    results = search_web(claim)
    trusted = sum(1 for r in results if r.get("source_type") in ("government", "education"))

    return jsonify({
        "claim": claim,
        "sources": results,
        "total_sources": len(results),
        "trusted_sources": trusted,
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
    })

@app.route("/search_trusted", methods=["POST"])
def search_trusted():
    """Alias for /search — kept for backwards compatibility."""
    return search()

@app.route("/health", methods=["GET"])
def health():
    return jsonify({
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "status": "alive",
        "browserbase": "configured" if BROWSERBASE_API_KEY else "not configured",
    })

@app.route("/info", methods=["GET"])
def info():
    return jsonify({
        "name": AGENT_NAME,
        "version": AGENT_VERSION,
        "port": 5001,
        "description": "Searches via Browserbase cloud browser, falls back to DuckDuckGo + Wikipedia APIs",
        "endpoints": {
            "POST /search": '{ "claim": "..." } → { "sources": [...] }',
            "GET /health": "Status check",
        },
    })

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"🔍 {AGENT_NAME} v{AGENT_VERSION}")
    print(f"📍 http://localhost:5001")
    print(f"🌐 Browserbase: {'✅ configured' if BROWSERBASE_API_KEY else '⚠️  not configured (will use API fallback)'}")
    print(f"📊 Arize AX space: {os.getenv('ARIZE_SPACE_ID', 'not set')}")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=5001, debug=False)
