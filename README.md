# Veritas

Veritas is a multi-agent fact-checking interface for crisis and news claims.
It searches current sources, extracts evidence, scores a claim, and plots each
source by stance, reliability, and relevance.

## Features

- Searcher, extractor, and judge agents
- Live streaming pipeline progress
- Automatic location, time, and claim-type extraction
- Deterministic evidence scoring when no Anthropic key is configured
- Evidence map with per-source stance and reliability
- Relevance-proportional circle areas

## Setup

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
```

Export any desired values from `.env`, then start all services:

```bash
./start_all.sh
```

Open <http://127.0.0.1:5000>.

Stop the services with:

```bash
./stop_all.sh
```

The Anthropic key is optional. Without it, Veritas uses its deterministic
evidence scorer.

## Services

- UI backend: `5000`
- Searcher agent: `5001`
- Extractor agent: `5002`
- Judge agent: `5003`

## Safety

Veritas is a triage aid, not an emergency authority. Confirm critical claims
through official channels before acting.
