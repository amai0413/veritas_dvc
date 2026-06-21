// Long-form analysis endpoint (Phase A). Decomposes a document into its central
// factual claims, runs each through the SAME verdict engine as /api/check, and
// aggregates them into a document-level credibility score.
//
// Observability: when Arize is configured the whole run is one trace —
//   veritas.analyze_document (CHAIN)
//     └─ veritas.decompose      (LLM)
//     └─ anthropic.verdict × N  (LLM, fanned out in parallel)
// The per-claim verdict spans are parented explicitly to the document root so
// the fan-out nests correctly even though they run concurrently.
//
// Env-gated exactly like /api/check: no ANTHROPIC_API_KEY -> deterministic mock.

import Anthropic from "@anthropic-ai/sdk";
import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import * as Sentry from "@sentry/nextjs";
import { getArize, OI } from "@/lib/arize";
import { MODEL, liveVerdict, mockVerdict } from "@/lib/verdict";
import { decomposeDocument, mockDecompose } from "@/lib/decompose";
import { scoreClaim, scoreDocument } from "@/lib/scoring";
import { fetchArticle, ArticleFetchError } from "@/lib/fetchArticle";
import type {
  DocumentAnalysis,
  DocumentClaim,
  DocumentSource,
  ExtractedClaim,
} from "@/lib/types";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";

const MAX_DOC_CHARS = 16000; // bound payload/context size

type VerdictLike = { aiVerdict: { score: number; reasoning: string; verifiable: boolean }; sources: DocumentClaim["sources"] };

// Turn extracted claims + their verdicts into a fully-scored document analysis.
// A failed verdict falls back to the mock so the document still renders whole.
function buildAnalysis(
  summary: string,
  note: string,
  items: { claim: ExtractedClaim; verdict: VerdictLike | null }[],
): DocumentAnalysis {
  const claims: DocumentClaim[] = items.map(({ claim, verdict }) => {
    const v = verdict ?? mockVerdict(claim.text);
    const scored = scoreClaim({
      text: claim.text,
      aiVerdict: v.aiVerdict,
      sources: v.sources,
      votes: { trusted: [], public: [] },
    });
    return { ...scored, quote: claim.quote, importance: claim.importance };
  });
  return { ...scoreDocument(claims, summary), note };
}

function mockAnalysis(document: string): DocumentAnalysis {
  const { summary, note, claims } = mockDecompose(document);
  return buildAnalysis(
    summary,
    note,
    claims.map((claim) => ({ claim, verdict: mockVerdict(claim.text) })),
  );
}

export async function POST(request: Request) {
  let document = "";
  let url = "";
  try {
    const body = await request.json();
    document = typeof body?.document === "string" ? body.document.trim() : "";
    url = typeof body?.url === "string" ? body.url.trim() : "";
  } catch {
    return Response.json({ error: "Invalid request body." }, { status: 400 });
  }

  // If a URL was given, fetch + extract the article first (no LLM key needed).
  // Fetch problems are user-facing 400s, not silent mocks.
  let source: DocumentSource | undefined;
  if (url) {
    try {
      const article = await fetchArticle(url);
      document = article.text;
      source = { title: article.title, url: article.url, siteName: article.siteName };
    } catch (e) {
      if (e instanceof ArticleFetchError) {
        return Response.json({ error: e.message }, { status: 400 });
      }
      Sentry.captureException(e, { tags: { route: "analyze", step: "fetch" } });
      return Response.json({ error: "Could not fetch that URL." }, { status: 502 });
    }
  }

  if (document.length < 40) {
    return Response.json(
      { error: "Please paste a longer passage (or a link) to analyze." },
      { status: 400 },
    );
  }
  document = document.slice(0, MAX_DOC_CHARS);

  // No key -> guaranteed mock.
  if (!process.env.ANTHROPIC_API_KEY) {
    return Response.json({ ...mockAnalysis(document), source, mock: true });
  }

  const client = new Anthropic();
  const arize = await getArize();

  // Runs the decompose + verdict fan-out. `trace?` wires each step into Arize
  // spans when provided; otherwise it's a plain pipeline.
  async function run(tracer?: NonNullable<Awaited<ReturnType<typeof getArize>>>["tracer"], parentCtx?: ReturnType<typeof trace.setSpan>) {
    // 1) Decompose the document into claims.
    let decomposed: { summary: string; note: string; claims: ExtractedClaim[] };
    if (tracer && parentCtx) {
      const s = tracer.startSpan("veritas.decompose", undefined, parentCtx);
      try {
        const r = await decomposeDocument(client, document);
        s.setAttribute(OI.SPAN_KIND, "LLM");
        s.setAttribute(OI.LLM_PROVIDER, "anthropic");
        s.setAttribute(OI.LLM_MODEL, MODEL);
        s.setAttribute(OI.INPUT_VALUE, document.slice(0, 4000));
        s.setAttribute(OI.OUTPUT_VALUE, JSON.stringify({ summary: r.summary, note: r.note, claims: r.claims.map((c) => c.text) }));
        s.setAttribute(OI.OUTPUT_MIME, "application/json");
        s.setAttribute(OI.TOK_PROMPT, r.promptTokens);
        s.setAttribute(OI.TOK_COMPLETION, r.completionTokens);
        s.setAttribute("veritas.claim_count", r.claims.length);
        s.setStatus({ code: SpanStatusCode.OK });
        decomposed = { summary: r.summary, note: r.note, claims: r.claims };
      } finally {
        s.end();
      }
    } else {
      decomposed = await decomposeDocument(client, document);
    }

    // 2) Verdict per claim, fanned out concurrently.
    const items = await Promise.all(
      decomposed.claims.map(async (claim) => {
        if (tracer && parentCtx) {
          const s = tracer.startSpan("anthropic.verdict", undefined, parentCtx);
          try {
            const v = await liveVerdict(client, claim.text, { effort: "low" });
            s.setAttribute(OI.SPAN_KIND, "LLM");
            s.setAttribute(OI.LLM_PROVIDER, "anthropic");
            s.setAttribute(OI.LLM_MODEL, MODEL);
            s.setAttribute(OI.INPUT_VALUE, claim.text);
            s.setAttribute(OI.OUTPUT_VALUE, JSON.stringify(v.aiVerdict));
            s.setAttribute(OI.OUTPUT_MIME, "application/json");
            s.setAttribute(OI.TOK_PROMPT, v.promptTokens);
            s.setAttribute(OI.TOK_COMPLETION, v.completionTokens);
            s.setAttribute(OI.TOK_TOTAL, v.promptTokens + v.completionTokens);
            s.setStatus({ code: SpanStatusCode.OK });
            return { claim, verdict: v as VerdictLike };
          } catch (e) {
            s.recordException(e as Error);
            s.setStatus({ code: SpanStatusCode.ERROR });
            return { claim, verdict: null };
          } finally {
            s.end();
          }
        }
        try {
          const v = await liveVerdict(client, claim.text, { effort: "low" });
          return { claim, verdict: v as VerdictLike };
        } catch {
          return { claim, verdict: null };
        }
      }),
    );

    return buildAnalysis(decomposed.summary, decomposed.note, items);
  }

  // Traced path.
  if (arize) {
    return arize.tracer.startActiveSpan("veritas.analyze_document", async (root) => {
      root.setAttribute(OI.SPAN_KIND, "CHAIN");
      root.setAttribute(OI.INPUT_VALUE, document.slice(0, 4000));
      root.setAttribute(OI.INPUT_MIME, "text/plain");
      const parentCtx = trace.setSpan(context.active(), root);
      try {
        const analysis = await run(arize.tracer, parentCtx);
        root.setAttribute(OI.OUTPUT_VALUE, JSON.stringify({ documentScore: Math.round(analysis.documentScore), counts: analysis.counts }));
        root.setAttribute("veritas.document_score", Math.round(analysis.documentScore));
        root.setStatus({ code: SpanStatusCode.OK });
        return Response.json({ ...analysis, source, mock: false });
      } catch (e) {
        root.recordException(e as Error);
        root.setStatus({ code: SpanStatusCode.ERROR });
        console.error("[analyze] failed:", e);
        Sentry.captureException(e, { tags: { route: "analyze" } });
        return Response.json({ ...mockAnalysis(document), source, mock: true });
      } finally {
        root.end();
        await arize.flush();
      }
    });
  }

  // Untraced path: live Claude, no Arize configured.
  try {
    const analysis = await run();
    return Response.json({ ...analysis, source, mock: false });
  } catch (e) {
    Sentry.captureException(e, { tags: { route: "analyze" } });
    return Response.json({ ...mockAnalysis(document), source, mock: true });
  }
}
