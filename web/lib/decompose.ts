// Claim decomposition — the front of the long-form pipeline. Takes a document
// (article, post, thread) and pulls out the central checkable factual claims,
// each restated as a self-contained sentence plus the verbatim quote it came
// from. Each claim then runs through the shared verdict engine in lib/verdict.

import type Anthropic from "@anthropic-ai/sdk";
import type { ExtractedClaim } from "./types";
import { MODEL, clamp, type Effort } from "./verdict";

// Bound cost/latency: a document fans out into at most this many verdict calls.
export const MAX_CLAIMS = 6;

const DECOMPOSE_SCHEMA = {
  type: "object",
  properties: {
    summary: {
      type: "string",
      description: "One sentence summarizing what the document is about / its overall thesis.",
    },
    note: {
      type: "string",
      description:
        "If the document is mostly opinion, anecdote, prediction, or otherwise has few or no verifiable factual claims, a one-sentence caveat saying so. Empty string if the document is substantive and factual.",
    },
    claims: {
      type: "array",
      description: `The ${MAX_CLAIMS} or fewer most central, independently verifiable factual claims. May be empty if the document has none.`,
      items: {
        type: "object",
        properties: {
          text: {
            type: "string",
            description:
              "A single, self-contained, verifiable factual claim, restated clearly with pronouns and context resolved.",
          },
          quote: {
            type: "string",
            description: "The shortest verbatim excerpt from the document this claim is based on.",
          },
          importance: {
            type: "number",
            description: "0 to 1 — how central this claim is to the document's thesis.",
          },
        },
        required: ["text", "quote", "importance"],
        additionalProperties: false,
      },
    },
  },
  required: ["summary", "note", "claims"],
  additionalProperties: false,
} as const;

const DECOMPOSE_SYSTEM = `You are Veritas's claim extractor. Extract the claims that have a VERIFIABLE FACTUAL CORE — anything evidence could bear on: statistics, causation, prevalence, comparisons, definitions, and CONTESTED EMPIRICAL ASSERTIONS — EVEN IF the author states them as a heated opinion. A charged claim like "most homeless people are addicts" or "giving money to panhandlers doesn't actually help" has a checkable core, so KEEP it — checking exactly these is the whole point of Veritas.

EXCLUDE only: pure value judgments with no factual content ("homeless people are disgusting", "this is immoral"), personal anecdotes ("I knew a guy who…", "I saw someone do…"), predictions about the future, and rhetorical questions.

Reword each kept claim into a NEUTRAL, self-contained, checkable sentence — strip the emotion and resolve pronouns/context so it stands alone (e.g. "Homeless people choose to be homeless" → "Most homelessness is a voluntary choice rather than driven by economic or health factors"). Order by importance; take at most ${MAX_CLAIMS}.

Always give a one-sentence "summary" of what the document is about. Set "note" to a one-sentence caveat ONLY when the document genuinely has almost nothing checkable (pure venting or storytelling); otherwise set "note" to an empty string. Do not pad the list with pure opinions or anecdotes to reach a count.`;

export type DecomposeResult = {
  summary: string;
  note: string;
  claims: ExtractedClaim[];
  promptTokens: number;
  completionTokens: number;
};

export async function decomposeDocument(
  client: Anthropic,
  document: string,
  opts: { effort?: Effort } = {},
): Promise<DecomposeResult> {
  const response = await client.messages.create({
    model: MODEL,
    // Headroom for medium-effort reasoning tokens + the JSON output.
    max_tokens: 6000,
    output_config: {
      // medium effort: the fact-vs-opinion judgment needs real reasoning.
      effort: opts.effort ?? "medium",
      format: { type: "json_schema", schema: DECOMPOSE_SCHEMA },
    },
    system: DECOMPOSE_SYSTEM,
    messages: [{ role: "user", content: `Document to analyze:\n\n"""\n${document}\n"""` }],
  });

  if (response.stop_reason === "refusal") throw new Error("model_refusal");
  const textBlock = response.content.find((b) => b.type === "text");
  if (!textBlock || textBlock.type !== "text") throw new Error("empty_response");

  const raw = JSON.parse(textBlock.text);
  const claims: ExtractedClaim[] = (Array.isArray(raw.claims) ? raw.claims : [])
    .slice(0, MAX_CLAIMS)
    .map((c: Record<string, unknown>) => ({
      text: String(c.text ?? "").trim(),
      quote: String(c.quote ?? "").trim(),
      importance: clamp(c.importance, 0, 1),
    }))
    .filter((c: ExtractedClaim) => c.text.length > 0);

  return {
    summary: String(raw.summary ?? ""),
    note: String(raw.note ?? ""),
    claims,
    promptTokens: response.usage?.input_tokens ?? 0,
    completionTokens: response.usage?.output_tokens ?? 0,
  };
}

// No-key fallback: a deterministic, always-renderable decomposition. Splits the
// document into sentences and treats the longest few as "claims".
export function mockDecompose(document: string): {
  summary: string;
  note: string;
  claims: ExtractedClaim[];
} {
  const sentences = document
    .split(/(?<=[.!?])\s+/)
    .map((s) => s.trim())
    .filter((s) => s.length > 25);

  const picked = [...sentences].sort((a, b) => b.length - a.length).slice(0, 3);
  const claims: ExtractedClaim[] = (picked.length ? picked : [document.slice(0, 160)]).map(
    (s, i) => ({
      text: s.slice(0, 200),
      quote: s.slice(0, 200),
      importance: 1 - i * 0.2,
    }),
  );

  return {
    summary:
      "Sample decomposition (no live model call): set ANTHROPIC_API_KEY for real claim extraction.",
    note: "",
    claims,
  };
}
