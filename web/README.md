# Veritas — The Human + AI Truth Engine

AI alone can't decide what's true. Veritas blends an **AI evidence-check** with
**human consensus** (trusted friends + crowd votes) into one weighted credibility
score — and shows the whole reasoning on a **2D evidence map** so users see *why*,
not just a verdict.

It works on a single claim **or a whole article** — paste a link and Veritas
fetches it, breaks it into its key factual claims, checks each one, and scores the
whole document. That's the difference between Veritas and a ChatGPT chat box: you
don't get one paragraph of prose, you get a **per-claim credibility map of the
document** — and every verdict is traced and continuously evaluated, not a black box.

**Phase 1** is the core product working end-to-end. **Phase 2** wraps it in real
observability — see [Observability & the feedback loop](#observability--the-feedback-loop)
for how every verdict is traced, evaluated, and improved (Arize AX + Sentry).

## What's working

- **AI verdict** — `POST /api/check` sends a claim to Claude (`claude-opus-4-8`,
  structured outputs) and returns `{ score, reasoning, sources[] }`.
- **Weighted scoring** — `lib/scoring.ts`: `finalScore = w_ai·aiScore + w_crowd·crowdScore`.
  Crowd weight **scales with vote volume** (few votes → AI dominates) and trusted
  votes outweigh public ones. Status resolves to Verified / False / Disputed / Uncertain.
- **Bottom line first** — a verdict badge + blended score + one sentence, above the graph.
- **2D evidence map** — `components/EvidenceMap.tsx`: each source plotted by
  stance (x) × reliability (y), colored by type, sized by relevance. The crowd
  consensus is plotted as one distinct marker on the *same* map. Points are clickable.
- **Live voting** — add votes and watch the crowd weight (and the map marker) update.
- **Long-form & URL analysis** — `POST /api/analyze` takes a pasted document *or a
  URL*, extracts the article, decomposes it into its key factual claims, checks each
  with the same engine, and returns a document-level score. See below.
- **Three demo scenarios** — Agreement, Conflict, Contested — pre-filled with
  sources *and* votes so the product tells a story with no live users.

## Long-form analysis — beyond a single claim

A single-claim checker is easy to dismiss as "just ask ChatGPT." The real problem is
a *document* full of claims — a news article, a viral thread, a press release — where
some statements are solid and some are quietly false. Veritas turns the whole thing
into a credibility map.

**Flow** (`POST /api/analyze`, UI: the **Analyze article** tab):

1. **Ingest** — paste text, or paste a **URL** and Veritas fetches it server-side and
   extracts the main article body with Mozilla Readability (the Firefox Reader engine).
   No copy-paste required; basic SSRF guards block private/loopback hosts.
2. **Decompose** — one Claude pass (`lib/decompose.ts`) pulls out the document's
   central, independently-checkable factual claims (up to 6), each restated as a
   standalone sentence plus the verbatim quote it came from.
3. **Check each claim** — every claim runs through the **same verdict engine** as
   single-claim mode (`lib/verdict.ts`), fanned out in parallel.
4. **Aggregate** — `scoreDocument()` combines the per-claim scores into one
   **importance-weighted** document score, with a status breakdown (how many claims
   were Verified / False / Disputed / Uncertain) and an expandable claim-by-claim view.

Because every claim is checked by the shared engine, the **Arize feedback loop and
the prompt calibration apply to long-form too** — and the whole document run shows up
in Arize as a single trace: `analyze_document → decompose → verdict × N`.

> Example: fed the Wikipedia article on *Moon landing conspiracy theories*, Veritas
> pulled 6 claims and scored them individually — the conspiracy framing landed near 0
> while the verifiable facts (orbiter photos, standing flags, the 411,000-person
> secrecy estimate) scored 85–99 — for an importance-weighted document score in the
> mid-60s. One paragraph from a chatbot can't show you that.

## Observability & the feedback loop

Most hackathon AI demos are black boxes: a prompt goes to an LLM, an answer comes
back, and nobody can see whether the answer was any good. Veritas is built the
opposite way — **every verdict is observable, scored, and used to make the next
one better.**

1. **Claude produces the verdict.** `POST /api/check` sends the claim to Claude
   (`claude-opus-4-8`, structured outputs) and gets back `{ score, reasoning, sources[] }`
   — the same engine that powers the 2D evidence map.

2. **Arize AX traces every claim-check run.** Each request opens an OpenTelemetry
   trace (OpenInference semantics) with three spans: a `check_claim` root, the
   `anthropic.verdict` LLM call (model, tokens, cost, input/output), and a second
   `evaluator` pass. You can replay any run in the Arize UI and see exactly what the
   model saw and said — no `console.log` archaeology.

3. **The *Veritas Verdict Calibration* evaluator grades each run.** An LLM-as-judge
   evaluator in Arize checks whether the AI's **numeric score and its reasoning are
   well-calibrated** — e.g. a debunked myth must land near 0, an established fact
   near 100, and a genuinely contested claim in the middle. It returns a
   `well_calibrated` / `poorly_calibrated` label *with an explanation* for every
   verdict, continuously.

4. **That makes a real feedback loop, not a ChatGPT wrapper.** The evaluator's
   explanations are exported back out, the recurring failures become concrete prompt
   fixes, and the next batch of runs is measurably better. In this build the
   evaluator caught a real bug — a non-claim input (`"test"`) was scored **50**
   ("genuinely contested") when it should have been flagged as unverifiable. We fed
   that explanation back into the system prompt; the same input now scores **3** with
   the reasoning *"not a verifiable factual claim."* **Observe → evaluate → improve**,
   on a loop.

> Both integrations are **env-gated**: with no keys set, the app behaves exactly as
> in Phase 1. Sentry captures any server/edge error (the `/api/check` failure paths
> report to it); Arize tracing and the evaluator activate only when the Arize keys
> are present. The demo never breaks for a missing key.

## Run it

```bash
npm install        # already done if scaffolded here
npm run dev        # http://localhost:3000
```

The **demo scenarios work with no API key.** To run **live** checks, add a key:

```bash
cp .env.local.example .env.local   # then edit ANTHROPIC_API_KEY
```

## Project map

```
app/page.tsx             UI: single-claim + article tabs, demos, voting
app/api/check/route.ts   single-claim verdict (+ Arize trace + evaluator)
app/api/analyze/route.ts long-form: fetch/decompose/verdict fan-out (traced)
lib/verdict.ts           the shared Claude verdict engine (prompt + schema)
lib/decompose.ts         splits a document into checkable claims
lib/fetchArticle.ts      URL fetch + Readability extraction (+ SSRF guard)
lib/types.ts             Source / Vote / Claim / DocumentAnalysis
lib/scoring.ts           weighted claim + document scoring (pure, testable)
lib/arize.ts             env-gated Arize AX tracing setup
lib/demoData.ts          three scripted scenarios
lib/display.ts           shared colors / labels
components/              ScorePanel, VerdictBadge, EvidenceMap, SourceCard, DocumentAnalysis
```

## Notes

- Sources for **live** checks are AI-estimated references for this transparency
  demo, not retrieved citations. (Phase 3 stretch: Browserbase real evidence gathering.)
- Scoring is pure TypeScript and runs on both server and client, so voting
  re-blends instantly without a round trip.
- Long-form analysis caps at 6 claims per document to bound latency/cost; URL
  fetching depends on the target site allowing it (some sites block bots — Veritas
  returns a readable error and you can paste the text instead).
