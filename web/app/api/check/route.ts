// AI evidence-check endpoint (Phase 1 + Phase 2).
//   - Phase 1: single route, Claude when ANTHROPIC_API_KEY is set, else
//     deterministic mock JSON (no Browserbase/Fetch.ai).
//   - Phase 2 (Arize AX): when ARIZE_API_KEY + ARIZE_SPACE_ID are set, the live
//     verdict call is traced to Arize and a second LLM "evaluator" pass scores
//     the verdict's quality (the evaluator loop). All env-gated; tracing failures
//     never break the response.
//
// The verdict engine itself lives in lib/verdict.ts so the long-form /api/analyze
// pipeline shares the exact same prompt and schema.

import Anthropic from "@anthropic-ai/sdk";
import { SpanStatusCode, context, trace } from "@opentelemetry/api";
import * as Sentry from "@sentry/nextjs";
import { getArize, OI } from "@/lib/arize";
import { MODEL, clamp, liveVerdict, mockVerdict, type VerdictResult } from "@/lib/verdict";

export const runtime = "nodejs";
export const dynamic = "force-dynamic"; // external call per request — never cache

const EVAL_SCHEMA = {
  type: "object",
  properties: {
    score: { type: "integer", description: "0-100 quality of the verdict's reasoning" },
    label: { type: "string", enum: ["pass", "weak", "fail"] },
    explanation: { type: "string", description: "One sentence justification" },
  },
  required: ["score", "label", "explanation"],
  additionalProperties: false,
} as const;

const EVAL_SYSTEM = `You are a strict evaluator of fact-check verdicts. Judge whether the verdict's reasoning is internally consistent with its numeric score and would hold up to scrutiny. Return a quality score (0-100), a label, and a one-sentence explanation.`;

type EvalResult = {
  score: number;
  label: string;
  explanation: string;
  promptTokens: number;
  completionTokens: number;
};

async function runEvaluator(
  client: Anthropic,
  claimText: string,
  verdict: VerdictResult,
): Promise<EvalResult> {
  const response = await client.messages.create({
    model: MODEL,
    max_tokens: 1000,
    output_config: {
      effort: "low",
      format: { type: "json_schema", schema: EVAL_SCHEMA },
    },
    system: EVAL_SYSTEM,
    messages: [
      {
        role: "user",
        content: `Claim: "${claimText}"\nVerdict score: ${verdict.aiVerdict.score}\nReasoning: ${verdict.aiVerdict.reasoning}`,
      },
    ],
  });

  const tb = response.content.find((b) => b.type === "text");
  const raw = tb && tb.type === "text" ? JSON.parse(tb.text) : {};
  return {
    score: Math.round(clamp(raw.score, 0, 100)),
    label: String(raw.label ?? "weak"),
    explanation: String(raw.explanation ?? ""),
    promptTokens: response.usage?.input_tokens ?? 0,
    completionTokens: response.usage?.output_tokens ?? 0,
  };
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

  // No key -> guaranteed mock.
  if (!process.env.ANTHROPIC_API_KEY) {
    return Response.json({ ...mockVerdict(claimText), mock: true });
  }

  const client = new Anthropic();
  const arize = await getArize();

  // Phase 2: traced path with evaluator loop (only when Arize is configured).
  if (arize) {
    return arize.tracer.startActiveSpan("veritas.check_claim", async (root) => {
      root.setAttribute(OI.SPAN_KIND, "CHAIN");
      root.setAttribute(OI.INPUT_VALUE, claimText);
      root.setAttribute(OI.INPUT_MIME, "text/plain");
      // Parent child spans EXPLICITLY to the root context so the trace tree nests
      // correctly on its own — no reliance on a globally-registered context manager.
      const parentCtx = trace.setSpan(context.active(), root);
      try {
        const verdict = await arize.tracer.startActiveSpan("anthropic.verdict", {}, parentCtx, async (s) => {
          try {
            const r = await liveVerdict(client, claimText, { effort: "low" });
            s.setAttribute(OI.SPAN_KIND, "LLM");
            s.setAttribute(OI.LLM_PROVIDER, "anthropic");
            s.setAttribute(OI.LLM_SYSTEM, "anthropic");
            s.setAttribute(OI.LLM_MODEL, MODEL);
            s.setAttribute(OI.INPUT_VALUE, claimText);
            s.setAttribute(OI.OUTPUT_VALUE, JSON.stringify(r.aiVerdict));
            s.setAttribute(OI.OUTPUT_MIME, "application/json");
            s.setAttribute(OI.TOK_PROMPT, r.promptTokens);
            s.setAttribute(OI.TOK_COMPLETION, r.completionTokens);
            s.setAttribute(OI.TOK_TOTAL, r.promptTokens + r.completionTokens);
            s.setStatus({ code: SpanStatusCode.OK });
            return r;
          } catch (e) {
            // Label the span even on failure so it never lands as an empty UNKNOWN.
            s.setAttribute(OI.SPAN_KIND, "LLM");
            s.setAttribute(OI.LLM_PROVIDER, "anthropic");
            s.setAttribute(OI.LLM_MODEL, MODEL);
            s.setAttribute(OI.INPUT_VALUE, claimText);
            s.recordException(e as Error);
            s.setStatus({ code: SpanStatusCode.ERROR });
            throw e;
          } finally {
            s.end();
          }
        });

        // Internal self-check — a second Claude pass that grades the verdict's
        // self-consistency. This is OURS (traced as veritas.self_check); it is
        // distinct from the Arize online LLM-as-judge "Veritas Verdict Calibration"
        // evaluator, which scores the trace from the Arize platform. Run inline so
        // the full 3-span trace (check_claim -> verdict -> self_check) is always
        // exported deterministically before the response — Arize is the priority.
        await arize.tracer.startActiveSpan("veritas.self_check", {}, parentCtx, async (s) => {
          try {
            const ev = await runEvaluator(client, claimText, verdict);
            s.setAttribute(OI.SPAN_KIND, "EVALUATOR");
            s.setAttribute(OI.LLM_MODEL, MODEL);
            s.setAttribute(OI.INPUT_VALUE, JSON.stringify(verdict.aiVerdict));
            s.setAttribute(OI.OUTPUT_VALUE, JSON.stringify(ev));
            s.setAttribute(OI.OUTPUT_MIME, "application/json");
            s.setAttribute(OI.TOK_PROMPT, ev.promptTokens);
            s.setAttribute(OI.TOK_COMPLETION, ev.completionTokens);
            s.setAttribute(OI.TOK_TOTAL, ev.promptTokens + ev.completionTokens);
            s.setAttribute("eval.verdict_quality.score", ev.score);
            s.setAttribute("eval.verdict_quality.label", ev.label);
            s.setAttribute("eval.verdict_quality.explanation", ev.explanation);
            s.setStatus({ code: SpanStatusCode.OK });
          } catch (e) {
            s.recordException(e as Error);
            s.setStatus({ code: SpanStatusCode.ERROR });
          } finally {
            s.end();
          }
        });

        root.setAttribute(OI.OUTPUT_VALUE, JSON.stringify(verdict.aiVerdict));
        root.setStatus({ code: SpanStatusCode.OK });
        return Response.json({
          aiVerdict: verdict.aiVerdict,
          sources: verdict.sources,
          mock: false,
        });
      } catch (e) {
        root.recordException(e as Error);
        root.setStatus({ code: SpanStatusCode.ERROR });
        Sentry.captureException(e, { tags: { phase: "traced", route: "check" } });
        return Response.json({ ...mockVerdict(claimText), mock: true });
      } finally {
        root.end();
        await arize.flush();
      }
    });
  }

  // Phase 1 path: live Claude, no tracing configured.
  try {
    const r = await liveVerdict(client, claimText, { effort: "low" });
    return Response.json({ aiVerdict: r.aiVerdict, sources: r.sources, mock: false });
  } catch (e) {
    Sentry.captureException(e, { tags: { phase: "untraced", route: "check" } });
    return Response.json({ ...mockVerdict(claimText), mock: true });
  }
}
