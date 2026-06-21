// Deep check — the agentic "find the links yourself" path.
//   claim -> Claude writes search queries -> web search for real result links
//         -> Browserbase scrapes the top pages -> verdict grounded in REAL excerpts.
// Unlike /api/check (sources are AI-estimated), here the sources are pages we
// actually retrieved. Fully traced in Arize: deep_check -> research -> verdict.
// Env-gated like the rest: no ANTHROPIC_API_KEY -> deterministic mock.

import Anthropic from "@anthropic-ai/sdk";
import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import * as Sentry from "@sentry/nextjs";
import { getArize, OI } from "@/lib/arize";
import { MODEL, liveVerdict, liveVerdictWithEvidence, mockVerdict } from "@/lib/verdict";
import { gatherEvidence, type Evidence } from "@/lib/research";

export const runtime = "nodejs";
export const dynamic = "force-dynamic";
export const maxDuration = 120; // search + multi-page scrape can take a while

type EvidenceMeta = { title: string; url: string; siteName?: string; query: string };

function meta(evidence: Evidence[]): EvidenceMeta[] {
  return evidence.map((e) => ({ title: e.title, url: e.url, siteName: e.siteName, query: e.query }));
}

export async function POST(request: Request) {
  let claimText = "";
  try {
    const body = await request.json();
    claimText = typeof body?.claim === "string" ? body.claim.trim() : "";
  } catch {
    return Response.json({ error: "Invalid request body." }, { status: 400 });
  }
  if (!claimText) {
    return Response.json({ error: "Please provide a claim to check." }, { status: 400 });
  }
  if (!process.env.ANTHROPIC_API_KEY) {
    return Response.json({ ...mockVerdict(claimText), queries: [], evidence: [], sourcesLive: false, mock: true });
  }

  const client = new Anthropic();
  const arize = await getArize();

  // The actual work, optionally wrapped in Arize spans.
  async function execute(
    tracer?: NonNullable<Awaited<ReturnType<typeof getArize>>>["tracer"],
    parentCtx?: ReturnType<typeof trace.setSpan>,
  ) {
    // 1) Agentic research: queries -> search -> scrape.
    let research: Awaited<ReturnType<typeof gatherEvidence>>;
    if (tracer && parentCtx) {
      const s = tracer.startSpan("veritas.research", undefined, parentCtx);
      try {
        research = await gatherEvidence(client, claimText);
        s.setAttribute(OI.SPAN_KIND, "RETRIEVER");
        s.setAttribute("tool.name", "browserbase+duckduckgo");
        s.setAttribute(OI.INPUT_VALUE, claimText);
        s.setAttribute("veritas.queries", JSON.stringify(research.queries));
        s.setAttribute("veritas.result_count", research.results.length);
        s.setAttribute("veritas.scraped_count", research.evidence.length);
        s.setAttribute(OI.OUTPUT_VALUE, JSON.stringify(meta(research.evidence)));
        s.setAttribute(OI.OUTPUT_MIME, "application/json");
        s.setStatus({ code: SpanStatusCode.OK });
      } catch (e) {
        s.setAttribute(OI.SPAN_KIND, "RETRIEVER");
        s.setAttribute(OI.INPUT_VALUE, claimText);
        s.recordException(e as Error);
        s.setStatus({ code: SpanStatusCode.ERROR });
        throw e;
      } finally {
        s.end();
      }
    } else {
      research = await gatherEvidence(client, claimText);
    }

    const haveEvidence = research.evidence.length > 0;

    // 2) Verdict — grounded in the scraped pages when we got any, else fall back
    //    to the model's own knowledge so the user still gets an answer.
    async function runVerdict() {
      return haveEvidence
        ? liveVerdictWithEvidence(client, claimText, research.evidence, { effort: "low" })
        : liveVerdict(client, claimText, { effort: "low" });
    }

    let verdict;
    if (tracer && parentCtx) {
      const s = tracer.startSpan("anthropic.verdict", undefined, parentCtx);
      try {
        verdict = await runVerdict();
        s.setAttribute(OI.SPAN_KIND, "LLM");
        s.setAttribute(OI.LLM_PROVIDER, "anthropic");
        s.setAttribute(OI.LLM_MODEL, MODEL);
        s.setAttribute(OI.INPUT_VALUE, claimText);
        s.setAttribute(OI.OUTPUT_VALUE, JSON.stringify(verdict.aiVerdict));
        s.setAttribute(OI.OUTPUT_MIME, "application/json");
        s.setAttribute(OI.TOK_PROMPT, verdict.promptTokens);
        s.setAttribute(OI.TOK_COMPLETION, verdict.completionTokens);
        s.setAttribute(OI.TOK_TOTAL, verdict.promptTokens + verdict.completionTokens);
        s.setAttribute("veritas.grounded", haveEvidence);
        s.setStatus({ code: SpanStatusCode.OK });
      } catch (e) {
        s.setAttribute(OI.SPAN_KIND, "LLM");
        s.setAttribute(OI.LLM_MODEL, MODEL);
        s.setAttribute(OI.INPUT_VALUE, claimText);
        s.recordException(e as Error);
        s.setStatus({ code: SpanStatusCode.ERROR });
        throw e;
      } finally {
        s.end();
      }
    } else {
      verdict = await runVerdict();
    }

    return {
      aiVerdict: verdict.aiVerdict,
      sources: verdict.sources,
      queries: research.queries,
      evidence: meta(research.evidence),
      sourcesLive: haveEvidence,
      mock: false,
    };
  }

  // Traced path.
  if (arize) {
    return arize.tracer.startActiveSpan("veritas.deep_check", async (root) => {
      root.setAttribute(OI.SPAN_KIND, "CHAIN");
      root.setAttribute(OI.INPUT_VALUE, claimText);
      root.setAttribute(OI.INPUT_MIME, "text/plain");
      const parentCtx = trace.setSpan(context.active(), root);
      try {
        const out = await execute(arize.tracer, parentCtx);
        root.setAttribute(OI.OUTPUT_VALUE, JSON.stringify(out.aiVerdict));
        root.setAttribute("veritas.sources_live", out.sourcesLive);
        root.setStatus({ code: SpanStatusCode.OK });
        return Response.json(out);
      } catch (e) {
        root.recordException(e as Error);
        root.setStatus({ code: SpanStatusCode.ERROR });
        Sentry.captureException(e, { tags: { phase: "traced", route: "deepcheck" } });
        return Response.json({ ...mockVerdict(claimText), queries: [], evidence: [], sourcesLive: false, mock: true });
      } finally {
        root.end();
        await arize.flush();
      }
    });
  }

  // Untraced path.
  try {
    return Response.json(await execute());
  } catch (e) {
    Sentry.captureException(e, { tags: { phase: "untraced", route: "deepcheck" } });
    return Response.json({ ...mockVerdict(claimText), queries: [], evidence: [], sourcesLive: false, mock: true });
  }
}
