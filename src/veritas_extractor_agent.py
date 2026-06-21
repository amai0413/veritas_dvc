"""
Veritas Extractor Agent v3.0
Standalone evidence extraction agent.

Priority chain:
  1. Browserbase (real cloud browser — handles JS-rendered pages, paywalls, anti-bot)
  2. requests + BeautifulSoup (lightweight fallback for simple HTML)

Tracked via Arize Phoenix OTEL.
Run: python veritas_extractor_agent.py
API: POST http://localhost:5002/extract  { "urls": ["https://..."] }
"""

import os
import time
import random
import requests
from flask import Flask, request, jsonify
from flask_cors import CORS
from bs4 import BeautifulSoup
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
        model_id="veritas-extractor-agent",
    )
    tracer = trace.get_tracer("veritas.extractor")
    print("✅ Arize AX tracing initialized")
except Exception as e:
    print(f"⚠️ Arize AX init warning: {e} — continuing without tracing")
    tracer = trace.get_tracer("veritas.extractor")

# ========== CONFIG ==========
BROWSERBASE_API_KEY = os.getenv("BROWSERBASE_API_KEY")
BROWSERBASE_PROJECT_ID = os.getenv("BROWSERBASE_PROJECT_ID")

app = Flask(__name__)
CORS(app)

AGENT_NAME = "Veritas Extractor Agent"
AGENT_VERSION = "3.0"

_BROWSER_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Referer": "https://www.google.com/",
}

# ========== 1. BROWSERBASE EXTRACTION (primary) ==========
def extract_with_browserbase(url: str) -> dict | None:
    """Open a real Browserbase session and extract clean text from any page."""
    if not BROWSERBASE_API_KEY or not BROWSERBASE_PROJECT_ID:
        return None
    try:
        from browserbase import Browserbase
        from playwright.sync_api import sync_playwright

        print(f"🌐 Browserbase: extracting {url}")
        bb = Browserbase(api_key=BROWSERBASE_API_KEY)
        session = bb.sessions.create(project_id=BROWSERBASE_PROJECT_ID)
        cdp_url = f"wss://connect.browserbase.com?apiKey={BROWSERBASE_API_KEY}&sessionId={session.id}"

        with sync_playwright() as pw:
            browser = pw.chromium.connect_over_cdp(cdp_url)
            context = browser.contexts[0] if browser.contexts else browser.new_context()
            page = context.pages[0] if context.pages else context.new_page()

            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            page.wait_for_timeout(2000)  # let JS settle

            result = page.evaluate("""
                () => {
                    // Remove noise
                    ['script','style','nav','footer','header','aside','form',
                     '.ad','[class*="cookie"]','[class*="popup"]','[id*="modal"]'
                    ].forEach(sel => {
                        document.querySelectorAll(sel).forEach(el => el.remove());
                    });

                    // Prefer semantic content containers
                    const candidates = [
                        'article', 'main', '[role="main"]',
                        '.content', '.post-content', '.article-content',
                        '.entry-content', '.main-content', '#content'
                    ];
                    for (const sel of candidates) {
                        const el = document.querySelector(sel);
                        if (el && el.innerText.length > 200) {
                            return { text: el.innerText.trim(), selector: sel };
                        }
                    }
                    // Fall back to full body
                    const body = document.body.innerText.replace(/\\s+/g, ' ').trim();
                    return { text: body, selector: 'body' };
                }
            """)
            title_el = page.title()
            browser.close()

        content = result.get("text", "")
        if len(content) < 100:
            return None

        return {
            "url": url,
            "title": title_el,
            "content": content[:5000],
            "length": len(content),
            "method": f"browserbase:{result.get('selector', 'unknown')}",
            "status": "success",
        }

    except ImportError:
        print("⚠️ playwright not installed. Run: pip install playwright && playwright install chromium")
        return None
    except Exception as e:
        print(f"⚠️ Browserbase extract error for {url}: {e}")
        return None

# ========== 2. REQUESTS + BEAUTIFULSOUP FALLBACK ==========
def _parse_content(html: str) -> str:
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
        tag.decompose()

    selectors = ["article", "main", ".content", ".post-content", ".article-content",
                 ".entry-content", ".main-content", "#content", "#main-content"]
    parts = []
    for sel in selectors:
        for el in soup.select(sel):
            text = el.get_text(separator=" ", strip=True)
            if len(text) > 100:
                parts.append(text)

    if not parts:
        text = soup.get_text(separator=" ", strip=True)
        text = " ".join(text.split())
        parts.append(text)

    return " ".join(" ".join(p.split()) for p in parts)

def extract_with_requests(url: str, retries: int = 2) -> dict | None:
    for attempt in range(retries + 1):
        try:
            time.sleep(random.uniform(0.3, 0.8))
            resp = requests.get(url, timeout=20, headers=_BROWSER_HEADERS, allow_redirects=True)

            if resp.status_code == 200:
                content = _parse_content(resp.text)
                if len(content) > 100:
                    print(f"✅ requests+BS4 extracted {len(content)} chars from {url}")
                    return {
                        "url": url,
                        "title": url,
                        "content": content[:5000],
                        "length": len(content),
                        "method": "requests+bs4",
                        "status": "success",
                    }

            elif resp.status_code == 403 and attempt < retries:
                time.sleep(3 + attempt * 2)
                continue
            elif resp.status_code == 404:
                print(f"⚠️ 404 Not Found: {url}")
                return None

        except requests.exceptions.Timeout:
            if attempt < retries:
                time.sleep(3)
        except requests.exceptions.ConnectionError:
            if attempt < retries:
                time.sleep(3)
        except Exception as e:
            print(f"⚠️ requests error for {url}: {e}")
            return None

    return None

# ========== MAIN EXTRACT PIPELINE ==========
def extract_url(url: str) -> dict | None:
    with tracer.start_as_current_span("extractor.extract_url") as span:
        span.set_attribute("url", url)

        # 1. Try Browserbase (handles JS, anti-bot)
        result = extract_with_browserbase(url)

        # 2. Fall back to requests + BS4
        if result is None:
            print(f"⚠️ Browserbase failed, using requests for {url}")
            result = extract_with_requests(url)

        if result:
            span.set_attribute("method", result.get("method", "unknown"))
            span.set_attribute("content_length", result.get("length", 0))
        else:
            span.set_attribute("error", "all_methods_failed")

        return result

def extract_urls(urls: list, limit: int = 5) -> list:
    evidence = []
    for url in urls[:limit]:
        result = extract_url(url)
        if result:
            evidence.append(result)
    return evidence

# ========== FLASK ROUTES ==========

@app.route("/extract", methods=["POST"])
def extract():
    data = request.json or {}
    urls = data.get("urls", [])
    if not urls:
        return jsonify({"error": "Provide a 'urls' list"}), 400

    print(f"\n{'='*50}\n📄 Extracting {len(urls)} URL(s)\n{'='*50}")
    evidence = extract_urls(urls)

    return jsonify({
        "evidence": evidence,
        "total_extracted": len(evidence),
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
    })

@app.route("/extract_single", methods=["POST"])
def extract_single():
    data = request.json or {}
    url = data.get("url", "").strip()
    if not url:
        return jsonify({"error": "Provide a 'url' field"}), 400

    result = extract_url(url)
    if result:
        return jsonify({**result, "agent": AGENT_NAME})
    return jsonify({"error": "Failed to extract content from that URL"}), 404

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
        "port": 5002,
        "description": "Extracts page content via Browserbase cloud browser (handles JS), falls back to requests+BeautifulSoup",
        "endpoints": {
            "POST /extract": '{ "urls": ["https://..."] } → { "evidence": [...] }',
            "POST /extract_single": '{ "url": "https://..." } → { content... }',
            "GET /health": "Status check",
        },
    })

if __name__ == "__main__":
    print(f"\n{'='*50}")
    print(f"📄 {AGENT_NAME} v{AGENT_VERSION}")
    print(f"📍 http://localhost:5002")
    print(f"🌐 Browserbase: {'✅ configured' if BROWSERBASE_API_KEY else '⚠️  not configured (will use requests fallback)'}")
    print(f"📊 Arize AX space: {os.getenv('ARIZE_SPACE_ID', 'not set')}")
    print(f"{'='*50}\n")
    app.run(host="0.0.0.0", port=5002, debug=False)
