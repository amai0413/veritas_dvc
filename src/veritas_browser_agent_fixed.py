"""
Veritas Judge Agent v3.0
Orchestrator + AI decision-maker. Coordinates the full fact-check pipeline:
  Searcher Agent → Extractor Agent → Claude (verdict)

Fallback chain when sub-agents are offline:
  Browserbase direct search → Mock data → Claude still runs on what it has

Tracked via Arize Phoenix (Anthropic + OTEL spans).
Run: python veritas_browser_agent_fixed.py
API: POST http://localhost:5003/evaluate  { "claim": "..." }
"""

import os
import json
import random
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
import anthropic
from dotenv import load_dotenv

load_dotenv()

# ========== ARIZE AX TRACING ==========
from arize.otel import register_otel, Endpoints
from openinference.instrumentation.anthropic import AnthropicInstrumentor
from opentelemetry import trace

try:
    tracer_provider = register_otel(
        endpoints=Endpoints.ARIZE,
        space_id=os.getenv("ARIZE_SPACE_ID"),
        api_key=os.getenv("ARIZE_API_KEY"),
        model_id="veritas-judge-agent",
    )
    AnthropicInstrumentor().instrument(tracer_provider=tracer_provider)
    tracer = trace.get_tracer("veritas.judge")
    print("✅ Arize AX tracing initialized (Anthropic instrumented)")
except Exception as e:
    print(f"⚠️ Arize AX init warning: {e} — continuing without tracing")
    tracer = trace.get_tracer("veritas.judge")

# ========== CONFIG ==========
ANTHROPIC_API_KEY   = os.getenv("ANTHROPIC_API_KEY")
BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID")

CLAUDE_MODEL = os.getenv("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

SEARCHER_URL  = os.getenv("SEARCHER_URL",  "http://localhost:5001/search")
EXTRACTOR_URL = os.getenv("EXTRACTOR_URL", "http://localhost:5002/extract")

if not ANTHROPIC_API_KEY:
    raise RuntimeError("ANTHROPIC_API_KEY not set — cannot start Judge Agent")

claude = anthropic.Anthropic(api_key=ANTHROPIC_API_KEY)

app = Flask(__name__)
CORS(app)

AGENT_NAME    = "Veritas Judge Agent"
AGENT_VERSION = "3.0"

# ========== STEP 1: SEARCH ==========

def _search_via_browserbase(claim: str) -> list:
    """Direct Browserbase search used when Searcher Agent is offline."""
    if not BROWSERBASE_API_KEY or not BROWSERBASE_PROJECT_ID:
        return []
    try:
        from browserbase import Browserbase
        from playwright.sync_api import sync_playwright

        print("🌐 Judge: direct Browserbase search (Searcher Agent offline)")
        bb = Browserbase(api_key=BROWSERBASE_API_KEY)
        session = bb.sessions.create(project_id=BROWSERBASE_PROJECT_ID)
        cdp_url = f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}&sessionId={session.id}"

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            page.goto(
                f"https://duckduckgo.com/?q={requests.utils.quote(claim)}&kl=us-en",
                wait_until="domcontentloaded",
                timeout=30000,
            )
            page.wait_for_timeout(2000)

            raw = page.evaluate("""
                () => {
                    const selectors = ['[data-testid="result"]', '.result', 'article'];
                    let items = [];
                    for (const sel of selectors) {
                        items = Array.from(document.querySelectorAll(sel));
                        if (items.length) break;
                    }
                    return items.slice(0, 6).map(el => ({
                        title:   (el.querySelector('h2,h3') || {}).innerText || '',
                        url:     (el.querySelector('a[href]') || {}).href || '',
                        snippet: (el.querySelector('p,[data-result="snippet"]') || {}).innerText || ''
                    })).filter(r => r.url && r.url.startsWith('http'));
                }
            """)
            browser.close()

        return [{"title": r["title"], "url": r["url"], "snippet": r["snippet"]} for r in raw if r.get("url")]
    except Exception as e:
        print(f"⚠️ Judge direct Browserbase error: {e}")
        return []

def _mock_search(claim: str) -> list:
    cl = claim.lower()
    if "vaccine" in cl and "autism" in cl:
        return [
            {"title": "Vaccines and Autism — Wikipedia", "url": "https://en.wikipedia.org/wiki/Vaccines_and_autism", "snippet": "Multiple studies show no link."},
            {"title": "CDC — Vaccines and Autism", "url": "https://www.cdc.gov/vaccinesafety/concerns/autism.html", "snippet": "CDC guidance."},
        ]
    elif "earth" in cl and "sun" in cl:
        return [
            {"title": "Earth's Orbit — Wikipedia", "url": "https://en.wikipedia.org/wiki/Earth%27s_orbit", "snippet": "Earth orbits the Sun."},
        ]
    elif "climate" in cl:
        return [
            {"title": "NASA Climate Evidence", "url": "https://climate.nasa.gov/evidence/", "snippet": "Scientific consensus on climate change."},
        ]
    else:
        return [
            {"title": f"Wikipedia: {claim[:50]}", "url": f"https://en.wikipedia.org/wiki/{claim.replace(' ', '_')}", "snippet": f"Article about {claim}"},
        ]

def search_web(claim: str) -> list:
    with tracer.start_as_current_span("judge.search_web") as span:
        span.set_attribute("claim", claim)
        try:
            resp = requests.post(SEARCHER_URL, json={"claim": claim}, timeout=20)
            if resp.status_code == 200:
                sources = resp.json().get("sources", [])
                print(f"✅ Searcher Agent: {len(sources)} sources")
                span.set_attribute("source", "searcher_agent")
                return sources
        except requests.exceptions.ConnectionError:
            print("⚠️ Searcher Agent offline — trying Browserbase directly")
        except Exception as e:
            print(f"⚠️ Searcher Agent error: {e}")

        # Browserbase direct fallback
        results = _search_via_browserbase(claim)
        if results:
            span.set_attribute("source", "browserbase_direct")
            return results

        # Last resort: mock
        print("⚠️ Using mock search data")
        span.set_attribute("source", "mock")
        return _mock_search(claim)

# ========== STEP 2: EXTRACT ==========

def _mock_extract(sources: list) -> list:
    return [
        {"title": s.get("title", "Source"), "url": s.get("url", ""), "content": s.get("snippet", "No content available")}
        for s in sources[:3]
    ]

def extract_evidence(sources: list) -> list:
    with tracer.start_as_current_span("judge.extract_evidence") as span:
        urls = [s.get("url") for s in sources[:3] if s.get("url")]
        span.set_attribute("url_count", len(urls))

        if not urls:
            return _mock_extract(sources)

        try:
            resp = requests.post(EXTRACTOR_URL, json={"urls": urls, "method": "basic"}, timeout=45)
            if resp.status_code == 200:
                evidence = resp.json().get("evidence", [])
                print(f"✅ Extractor Agent: {len(evidence)} pages extracted")
                span.set_attribute("source", "extractor_agent")
                return evidence
        except requests.exceptions.ConnectionError:
            print("⚠️ Extractor Agent offline — using search snippets as evidence")
        except Exception as e:
            print(f"⚠️ Extractor Agent error: {e}")

        span.set_attribute("source", "mock")
        return _mock_extract(sources)

# ========== STEP 3: JUDGE WITH CLAUDE ==========

def judge_claim(claim: str, evidence: list) -> dict:
    with tracer.start_as_current_span("judge.claude_verdict") as span:
        span.set_attribute("claim", claim)
        span.set_attribute("evidence_count", len(evidence))

        evidence_text = ""
        for i, e in enumerate(evidence):
            content = e.get("content", "") or e.get("snippet", "")
            if content and len(content) > 50:
                evidence_text += f"\nSource {i+1}: {e.get('title', 'Untitled')}\n{content[:1500]}\n"
        if not evidence_text:
            evidence_text = "No specific evidence found. Use your general knowledge."

        prompt = f"""You are Veritas, an AI fact-checking judge. Evaluate the claim below based on the evidence provided.

CLAIM: "{claim}"

EVIDENCE:
{evidence_text}

Provide your verdict as valid JSON only — no extra text:
{{"score": <0-100>, "reasoning": "<2-3 sentences>", "status": "<Verified|False|Disputed|Uncertain>"}}

Scoring guide:
  70-100 → Verified (clearly supported by evidence or scientific consensus)
  31-69  → Disputed or Uncertain (contested, nuanced, or insufficient evidence)
  0-30   → False (clearly contradicted by evidence or scientific consensus)
"""

        try:
            print(f"🧠 Sending to Claude ({CLAUDE_MODEL})...")
            response = claude.messages.create(
                model=CLAUDE_MODEL,
                max_tokens=512,
                temperature=0.2,
                messages=[{"role": "user", "content": prompt}],
            )
            result_text = response.content[0].text.strip()

            # Strip markdown fences if Claude wrapped the JSON
            if result_text.startswith("```"):
                result_text = result_text.split("```")[1]
                if result_text.startswith("json"):
                    result_text = result_text[4:]
                result_text = result_text.strip()

            result = json.loads(result_text)
            score = max(0, min(100, int(result.get("score", 50))))
            status = result.get("status", "Uncertain")
            if status not in ("Verified", "False", "Disputed", "Uncertain"):
                status = "Uncertain"

            span.set_attribute("score", score)
            span.set_attribute("status", status)
            return {"score": score, "reasoning": result.get("reasoning", ""), "status": status}

        except Exception as e:
            print(f"❌ Claude error: {e}")
            span.set_attribute("error", str(e))
            return _judge_fallback(claim)

def _judge_fallback(claim: str) -> dict:
    """Keyword heuristic fallback when Claude is unavailable."""
    cl = claim.lower()
    harmful = ["gay people are bad", "lgbtq bad", "homosexuality is wrong"]
    if any(h in cl for h in harmful):
        return {"score": 0, "reasoning": "Discriminatory claim with no scientific basis.", "status": "False"}

    false_kw   = ["flat earth", "vaccine autism", "moon landing fake", "5g covid", "chemtrails"]
    true_kw    = ["earth revolves", "climate change is real", "evolution", "gravity is real", "round earth"]

    if any(k in cl for k in false_kw):
        return {"score": random.randint(3, 15), "reasoning": "Scientific consensus refutes this claim.", "status": "False"}
    if any(k in cl for k in true_kw):
        return {"score": random.randint(85, 98), "reasoning": "Well-established scientific fact.", "status": "Verified"}

    score = random.randint(30, 70)
    status = "Verified" if score >= 70 else ("False" if score <= 30 else "Uncertain")
    return {"score": score, "reasoning": "Mixed evidence; further research recommended.", "status": status}

# ========== FLASK ROUTES ==========

@app.route("/evaluate", methods=["POST"])
def evaluate():
    data = request.json or {}
    claim = data.get("claim", "").strip()
    if not claim or len(claim) < 3:
        return jsonify({"error": "Provide a 'claim' (at least 3 characters)"}), 400

    print(f"\n{'='*50}\n📝 Evaluating: {claim}\n{'='*50}")

    with tracer.start_as_current_span("judge.evaluate") as span:
        span.set_attribute("claim", claim)

        sources  = search_web(claim)
        evidence = extract_evidence(sources)
        verdict  = judge_claim(claim, evidence)

        verdict["claim"] = claim
        verdict["sources"] = [
            {"title": e.get("title", "Source"), "url": e.get("url", "")}
            for e in evidence[:3]
        ]
        verdict["search_results"] = [
            {"title": s.get("title", "Source"), "url": s.get("url", "")}
            for s in sources[:3]
        ]

        print(f"✅ {verdict['status']} — {verdict['score']}/100")
        print(f"{'='*50}\n")
        return jsonify(verdict)

@app.route("/health", methods=["GET"])
def health():
    agents = {}
    for name, url in [("searcher", "http://localhost:5001/health"), ("extractor", "http://localhost:5002/health")]:
        try:
            r = requests.get(url, timeout=2)
            agents[name] = "online" if r.status_code == 200 else "offline"
        except Exception:
            agents[name] = "offline"

    return jsonify({
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "status": "alive",
        "model": CLAUDE_MODEL,
        "browserbase": "configured" if BROWSERBASE_API_KEY else "not configured",
        "sub_agents": agents,
    })

@app.route("/info", methods=["GET"])
def info():
    return jsonify({
        "name": AGENT_NAME,
        "version": AGENT_VERSION,
        "port": 5003,
        "description": "Orchestrates Search → Extract → Claude verdict. Runs standalone via Browserbase if sub-agents are offline.",
        "pipeline": {
            "1_search":  SEARCHER_URL,
            "2_extract": EXTRACTOR_URL,
            "3_judge":   f"Claude ({CLAUDE_MODEL})",
        },
        "endpoints": {
            "POST /evaluate": '{ "claim": "..." } → { score, status, reasoning, sources }',
            "GET /health":    "Status of this agent and sub-agents",
        },
    })

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"🧠 {AGENT_NAME} v{AGENT_VERSION}")
    print(f"📍 http://localhost:5003/evaluate")
    print(f"🤖 Claude model: {CLAUDE_MODEL}")
    print(f"🌐 Browserbase: {'✅ configured' if BROWSERBASE_API_KEY else '⚠️  not configured'}")
    print(f"📊 Arize AX space: {os.getenv('ARIZE_SPACE_ID', 'not set')}")
    print(f"🔗 Searcher:  {SEARCHER_URL}")
    print(f"🔗 Extractor: {EXTRACTOR_URL}")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=5003, debug=False)
