from flask import Flask, request, jsonify
from flask_cors import CORS
import requests
from bs4 import BeautifulSoup
import time
import random

app = Flask(__name__)
CORS(app)

# ========== AGENT IDENTITY ==========
AGENT_NAME = "Veritas Extractor Agent"
AGENT_VERSION = "2.0"

# ========== SMART HEADERS (Looks like a real browser) ==========
def get_headers():
    """Return headers that mimic a real Chrome browser"""
    return {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.9',
        # Only advertise encodings requests can decode natively. Brotli ("br")
        # needs the optional `brotli` package; without it, br responses come back
        # as raw compressed bytes and turn into mojibake/garbage.
        'Accept-Encoding': 'gzip, deflate',
        'Connection': 'keep-alive',
        'Upgrade-Insecure-Requests': '1',
        'Sec-Fetch-Dest': 'document',
        'Sec-Fetch-Mode': 'navigate',
        'Sec-Fetch-Site': 'none',
        'Cache-Control': 'max-age=0',
        'Referer': 'https://www.google.com/',
    }

# ========== SMART DELAY (Avoid rate limiting) ==========
def smart_delay():
    """Random delay to avoid being blocked"""
    delay = random.uniform(0.5, 1.5)
    time.sleep(delay)

# ========== EXTRACT CONTENT WITH RETRIES ==========
def extract_content(urls, max_retries=2):
    """Visit URLs and extract content with retry logic"""
    evidence = []

    for i, url in enumerate(urls[:5]):
        success = False
        for attempt in range(max_retries + 1):
            try:
                print(f"📄 Visiting ({i+1}/{len(urls[:5])}): {url}")

                # Get fresh headers for each request
                headers = get_headers()

                # Send GET request with timeout
                response = requests.get(url, timeout=20, headers=headers, allow_redirects=True)

                # Check if request was successful
                if response.status_code == 200:
                    # Parse HTML
                    # Honor the real charset (Japanese pages are often Shift-JIS/
                    # EUC-JP); requests falls back to ISO-8859-1 and mangles them.
                    if not response.encoding or response.encoding.lower() == 'iso-8859-1':
                        response.encoding = response.apparent_encoding
                    soup = BeautifulSoup(response.text, 'html.parser')

                    # Remove unwanted elements
                    for element in soup(["script", "style", "nav", "footer", "header", "aside", "form"]):
                        element.decompose()

                    # Try to find main content
                    content = extract_main_content(soup)

                    if content and len(content) > 100:
                        evidence.append({
                            "url": url,
                            "content": content[:5000],
                            "length": len(content),
                            "status": "success"
                        })
                        print(f"✅ Extracted {len(content)} characters")
                        success = True
                        break
                    else:
                        print(f"⚠️ Content too short ({len(content)} chars), retrying...")

                elif response.status_code == 403:
                    print(f"⚠️ 403 Forbidden, attempt {attempt + 1}/{max_retries + 1}")
                    if attempt < max_retries:
                        # Wait longer and try with different headers
                        time.sleep(3 + attempt * 2)
                        continue

                elif response.status_code == 404:
                    print(f"⚠️ 404 Not Found, skipping")
                    break

                else:
                    print(f"⚠️ Status {response.status_code}, attempt {attempt + 1}")
                    if attempt < max_retries:
                        time.sleep(2)
                        continue

            except requests.exceptions.Timeout:
                print(f"⚠️ Timeout, attempt {attempt + 1}")
                if attempt < max_retries:
                    time.sleep(3)
                    continue

            except requests.exceptions.ConnectionError:
                print(f"⚠️ Connection error, attempt {attempt + 1}")
                if attempt < max_retries:
                    time.sleep(3)
                    continue

            except Exception as e:
                print(f"⚠️ Error: {e}")
                break

        # Smart delay between different URLs
        smart_delay()

    return evidence

# ========== EXTRACT MAIN CONTENT ==========
def extract_main_content(soup):
    """Extract the main text content from a page"""

    # Try different content selectors in order of preference
    content_selectors = [
        'article',
        'main',
        '.content',
        '.post-content',
        '.article-content',
        '.entry-content',
        '.main-content',
        '#content',
        '#main-content',
        '.body-text',
        '.text-content'
    ]

    content_parts = []

    # Try to find content using selectors
    for selector in content_selectors:
        elements = soup.select(selector)
        if elements:
            for element in elements:
                text = element.get_text(separator=' ', strip=True)
                if len(text) > 100:
                    content_parts.append(text)

    # If no content found with selectors, get all text
    if not content_parts:
        # Get all text and clean it up
        text = soup.get_text(separator=' ', strip=True)

        # Remove extra whitespace
        lines = (line.strip() for line in text.splitlines())
        chunks = (phrase.strip() for line in lines for phrase in line.split("  "))
        text = ' '.join(chunk for chunk in chunks if chunk)

        if len(text) > 100:
            content_parts.append(text)

    # Combine all parts
    combined_content = ' '.join(content_parts)

    # Clean up extra spaces
    combined_content = ' '.join(combined_content.split())

    return combined_content

# ========== EXTRACT WITH FALLBACKS ==========
def extract_with_fallbacks(urls):
    """Extract content using multiple fallback strategies"""
    evidence = []

    for url in urls[:3]:
        try:
            print(f"📄 Trying fallback extraction for: {url}")

            # Try with different User-Agents
            user_agents = [
                'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36',
                'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'
            ]

            for ua in user_agents[:2]:
                headers = {'User-Agent': ua}
                response = requests.get(url, timeout=15, headers=headers)

                if response.status_code == 200:
                    # Honor the real charset (Japanese pages are often Shift-JIS/
                    # EUC-JP); requests falls back to ISO-8859-1 and mangles them.
                    if not response.encoding or response.encoding.lower() == 'iso-8859-1':
                        response.encoding = response.apparent_encoding
                    soup = BeautifulSoup(response.text, 'html.parser')
                    for element in soup(["script", "style", "nav", "footer", "header"]):
                        element.decompose()

                    text = soup.get_text(separator=' ', strip=True)
                    text = ' '.join(text.split())

                    if len(text) > 100:
                        evidence.append({
                            "url": url,
                            "content": text[:5000],
                            "length": len(text),
                            "status": "success_fallback"
                        })
                        print(f"✅ Extracted {len(text)} characters with fallback")
                        break

            smart_delay()

        except Exception as e:
            print(f"⚠️ Fallback error for {url}: {e}")

    return evidence

# ========== API ENDPOINTS ==========

@app.route('/extract', methods=['POST'])
def extract():
    """Extract content from provided URLs"""
    try:
        data = request.json
        urls = data.get('urls', [])
        method = data.get('method', 'basic')
        selectors = data.get('selectors', None)

        if not urls:
            return jsonify({"error": "Please provide URLs"}), 400

        print(f"\n{'='*50}")
        print(f"📄 Extracting from {len(urls)} URLs")
        print(f"📋 Method: {method}")
        print(f"{'='*50}")

        # Try primary extraction
        evidence = extract_content(urls)

        # If no content extracted, try fallbacks
        if len(evidence) == 0:
            print("⚠️ Primary extraction failed, trying fallbacks...")
            evidence = extract_with_fallbacks(urls)

        print(f"✅ Extracted from {len(evidence)} sources")
        print(f"{'='*50}\n")

        return jsonify({
            "evidence": evidence,
            "total_extracted": len(evidence),
            "agent": AGENT_NAME,
            "version": AGENT_VERSION
        })

    except Exception as e:
        print(f"❌ Error: {e}")
        return jsonify({"error": str(e)}), 500

@app.route('/extract_single', methods=['POST'])
def extract_single():
    """Extract content from a single URL"""
    try:
        data = request.json
        url = data.get('url', '').strip()

        if not url:
            return jsonify({"error": "Please provide a URL"}), 400

        print(f"\n📄 Extracting single URL: {url}")

        evidence = extract_content([url])

        if evidence:
            return jsonify({
                "url": url,
                "content": evidence[0]['content'],
                "length": evidence[0]['length'],
                "status": evidence[0].get('status', 'success'),
                "agent": AGENT_NAME
            })
        else:
            return jsonify({"error": "Failed to extract content"}), 404

    except Exception as e:
        return jsonify({"error": str(e)}), 500

@app.route('/health', methods=['GET'])
def health():
    return jsonify({
        "agent": AGENT_NAME,
        "version": AGENT_VERSION,
        "status": "alive",
        "message": "Ready to extract content from websites!"
    })

@app.route('/info', methods=['GET'])
def info():
    return jsonify({
        "name": AGENT_NAME,
        "version": AGENT_VERSION,
        "description": "Extracts content from URLs with anti-blocking measures",
        "endpoints": {
            "extract": "POST /extract - Extract from multiple URLs",
            "extract_single": "POST /extract_single - Extract from one URL",
            "health": "GET /health - Check agent status"
        }
    })

if __name__ == '__main__':
    print("\n" + "="*50)
    print(f"📄 {AGENT_NAME} v{AGENT_VERSION}")
    print("="*50)
    print("📍 Running on: http://localhost:5002")
    print("🔗 Endpoint: http://localhost:5002/extract")
    print("🔗 Single: http://localhost:5002/extract_single")
    print("📊 Health: http://localhost:5002/health")
    print("="*50)
    print("🛡️ Anti-blocking measures active:")
    print("   • Browser-like headers")
    print("   • Random delays")
    print("   • Retry logic")
    print("   • Multiple User-Agents")
    print("   • Smart content extraction")
    print("="*50)
    print("⚡ Ready to extract evidence!\n")

    app.run(host='0.0.0.0', port=5002, debug=False, use_reloader=False)