from flask import Flask, request, jsonify, send_from_directory, Response, stream_with_context
from flask_cors import CORS
import os
import json
import requests

app = Flask(__name__, static_folder=".", static_url_path="")
CORS(app)

FRIEND_AGENT_URL = os.environ.get(
    "FRIEND_AGENT_URL",
    "https://mobilize-amber-trickery.ngrok-free.dev"
).rstrip("/")

@app.get("/")
def index():
    return send_from_directory(".", "index.html")

@app.get("/health")
def health():
    try:
        r = requests.get(
            f"{FRIEND_AGENT_URL}/health",
            headers={"ngrok-skip-browser-warning": "true"},
            timeout=10
        )
        return jsonify({
            "local_backend": "alive",
            "friend_agent_status": r.status_code,
            "friend_agent_response": safe_json(r)
        })
    except Exception as e:
        return jsonify({
            "local_backend": "alive",
            "friend_agent_status": "unreachable",
            "error": str(e)
        }), 502

@app.post("/api/check")
def api_check():
    payload = request.get_json(silent=True) or {}

    claim = (
        payload.get("input")
        or payload.get("claim")
        or payload.get("text")
        or ""
    ).strip()

    context = payload.get("context") or {}

    if not claim:
        return jsonify({"error": "missing_claim"}), 400

    try:
        r = requests.post(
            f"{FRIEND_AGENT_URL}/evaluate",
            json={
                "claim": claim,
                "context": context
            },
            headers={"ngrok-skip-browser-warning": "true"},
            timeout=35
        )

        return jsonify({
            "source": "friend_agent",
            "status_code": r.status_code,
            "raw": safe_json(r)
        }), r.status_code

    except Exception as e:
        return jsonify({
            "error": "friend_agent_unreachable",
            "message": str(e)
        }), 502

@app.post("/api/extract-context")
def api_extract_context():
    payload = request.get_json(silent=True) or {}
    claim = (payload.get("input") or payload.get("claim") or payload.get("text") or "").strip()
    empty = {"location": None, "datetime": None, "claim_type": "other"}
    if not claim:
        return jsonify(empty)
    try:
        r = requests.post(
            f"{FRIEND_AGENT_URL}/extract-context",
            json={"claim": claim},
            headers={"ngrok-skip-browser-warning": "true"},
            timeout=20,
        )
        return jsonify(safe_json(r)), r.status_code
    except Exception as e:
        empty["error"] = str(e)
        return jsonify(empty), 200

@app.post("/api/check/stream")
def api_check_stream():
    payload = request.get_json(silent=True) or {}

    claim = (
        payload.get("input")
        or payload.get("claim")
        or payload.get("text")
        or ""
    ).strip()

    context = payload.get("context") or {}

    if not claim:
        return jsonify({"error": "missing_claim"}), 400

    def generate():
        try:
            with requests.post(
                f"{FRIEND_AGENT_URL}/evaluate/stream",
                json={"claim": claim, "context": context},
                headers={"ngrok-skip-browser-warning": "true"},
                stream=True,
                timeout=90,
            ) as r:
                # Re-emit each SSE line, restoring the blank-line framing that
                # iter_lines() strips, so the browser sees valid SSE messages.
                for line in r.iter_lines(decode_unicode=True):
                    yield (line if line else "") + "\n"
        except Exception as e:
            yield f"data: {json.dumps({'stage': 'error', 'message': str(e)})}\n\n"

    return Response(
        stream_with_context(generate()),
        mimetype="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )

def safe_json(response):
    try:
        return response.json()
    except Exception:
        return {"text": response.text}

if __name__ == "__main__":
    app.run(
        host="0.0.0.0",
        port=int(os.environ.get("PORT", "5000")),
        debug=False,
        use_reloader=False,
    )
