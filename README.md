# Veritas — Crisis Information Triage

> We are not trying to build an AI that decides truth. We are building a crisis
> information triage system that separates confirmed facts, unverified claims,
> contradictions, official silence, and urgent risks under uncertainty.

Veritas is a multi-agent system for triaging crisis and news claims (disasters,
shootings, terrorism, infrastructure failures, breaking news). It searches
current sources, extracts evidence, and returns **structured uncertainty** — not
an absolute truth verdict. The headline number is an **evidence-confidence
score** (how strong the current evidence is), not a probability of truth.

## What it does

- **Crisis triage status** (not just true/false): `Verified`, `Likely true`,
  `Unconfirmed`, `Conflicting reports`, `Likely false`, `False`,
  `Urgent but unverified`. "No official source found" is never treated as false.
- **Evidence confidence (0–100)** — strength of current evidence, shown as
  "evidence confidence, not absolute truth".
- **Claim decomposition** — each claim is broken into sub-claims (event,
  location, time, magnitude, casualties, …), each with its own status/confidence.
- **Multiple independent claims** — if one input holds several distinct claims,
  each is verified separately (in parallel) and shown as its own card with a
  **Details** view (full 2D evidence map, sub-claims, sources).
- **Official confirmation tracking** — `official_confirmation`
  (confirmed / contradicted / not_found / unclear), `official_silence_risk`,
  `media_only`, `social_only`.
- **Context-aware search** — user/auto-detected location, time, incident type and
  language steer the search queries and which authoritative feeds are used.
- **Authoritative hazard feeds (no key)** — USGS (earthquakes), NASA EONET
  (wildfire/volcano/storm), GDACS (global disaster alerts), routed by incident
  type; always backed by latest news (Google News, GDELT) + Wikipedia.
- **Relevance-based source selection** — only the sources actually relevant to a
  claim are used, so the source count varies by case instead of a fixed number.
- **URL mode** — paste a link and Veritas fetches the page, extracts the main
  claim, and verifies *that* (keeping `original_source` separate from the
  verification sources). Basic SSRF guards; `itest.5ch.io` is normalized to the
  static `5ch.net` thread.
- **Multilingual (Lv1)** — detects the claim's language, searches in the local
  locale, and writes the verdict text in that language (enum values stay English).
- **Evidence map** — X = stance (disputes ◄ ► supports the claim),
  Y = source reliability, circle size = relevance, color = source type.
- **Live streaming pipeline** with real per-stage progress.
- **Arize / Phoenix tracing (opt-in)** — the time/location extraction and the
  triage call emit OpenTelemetry trace "signals" with the predicted values as
  span attributes, so an Arize/Phoenix dashboard shows what the model predicted.
- **Deterministic fallback** — works without an Anthropic key (rule-based scorer).

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env   # then edit .env
./start_all.sh
```

Open <http://127.0.0.1:5000>. Stop with `./stop_all.sh`.

## Production deployment

The production container runs all four Flask applications together:

- public UI on `$PORT`
- Searcher on internal port `5001`
- Extractor on internal port `5002`
- Judge on internal port `5003`

Build and run locally:

```bash
docker build -t veritas-crisis-mode .
docker run --rm -p 10000:10000 \
  -e ANTHROPIC_API_KEY \
  -e PHOENIX_API_KEY \
  -e ARIZE_SPACE_ID \
  -e PHOENIX_COLLECTOR_ENDPOINT=https://otlp.arize.com/v1/traces \
  -e ENABLE_PHOENIX=1 \
  veritas-crisis-mode
```

The included `render.yaml` deploys the same container on Render. Add secret
values in the Render dashboard; never commit `.env`.

## Configuration (`.env`)

| Variable | Purpose |
|---|---|
| `ANTHROPIC_API_KEY` | Enables the LLM judge/extraction (optional; falls back to a deterministic scorer). |
| `VERITAS_TRIAGE_MODEL` | Model for the core triage judgment (default `claude-sonnet-4-6`). |
| `VERITAS_FAST_MODEL` | Model for extraction-style calls (default `claude-haiku-4-5`). |
| `ENABLE_PHOENIX` | `1` to send traces to Arize/Phoenix. |
| `PHOENIX_API_KEY`, `PHOENIX_COLLECTOR_ENDPOINT` | Arize/Phoenix tracing credentials and endpoint. |
| `ARIZE_SPACE_ID` | Required Space ID when sending traces to Arize AX. |

## Services

- UI backend: `5000`
- Searcher agent: `5001`
- Extractor agent: `5002`
- Judge agent: `5003`

## Architecture

Browser → UI backend (`app.py`, proxies + SSE) → Judge (`5003`) which orchestrates
Searcher (`5001`) + Extractor (`5002`) and runs the crisis-triage analysis. The
**backend is the single source of truth** for the verdict; the frontend only
renders it.

## Limitations

- Cannot guarantee absolute truth; early disaster/incident information is often
  incomplete.
- Official sources can be delayed or biased by region/politics.
- Source-independence detection and human-consensus weighting are not yet built
  (Phase 3 TODO); time-phase source weighting is Phase 4 TODO.
- Multilingual place-name matching against English feeds is approximate.
- JavaScript-rendered pages can't be read by the static extractor (handled
  gracefully; `5ch` is special-cased).

## Safety

Veritas is a triage aid, **not** an emergency authority. Do not use these scores
for real dispatch, evacuation, or resource-allocation decisions. Always confirm
critical claims through official channels before acting.
