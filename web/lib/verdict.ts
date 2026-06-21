// The Veritas verdict engine — the single Claude call that turns one factual
// claim into a scored, sourced verdict. Extracted from the check route so that
// BOTH the single-claim endpoint and the long-form /api/analyze pipeline share
// exactly the same prompt and schema (so prompt improvements apply everywhere).

import type Anthropic from "@anthropic-ai/sdk";
import type { Source } from "./types";

export const MODEL = "claude-opus-4-8";

export const VALID_TYPES = ["gov", "academic", "news", "blog", "social"];

const SOURCE_SCHEMA = {
  type: "object",
  properties: {
    title: { type: "string", description: "Short source title" },
    url: { type: "string", description: "A plausible URL for the source" },
    type: { type: "string", enum: VALID_TYPES, description: "Source category" },
    date: { type: "string", description: "Year or ISO date; empty string if unknown" },
    stance: {
      type: "number",
      description: "-1 fully contradicts, 0 neutral, +1 fully supports the claim",
    },
    reliability: { type: "number", description: "0 low to 1 high" },
    relevance: { type: "number", description: "0 to 1 relevance to the claim" },
    summary: { type: "string", description: "One sentence on what the source says" },
    whyItMatters: { type: "string", description: "One sentence on why it bears on the claim" },
  },
  required: [
    "title",
    "url",
    "type",
    "date",
    "stance",
    "reliability",
    "relevance",
    "summary",
    "whyItMatters",
  ],
  additionalProperties: false,
} as const;

export const VERDICT_SCHEMA = {
  type: "object",
  properties: {
    verifiable: {
      type: "boolean",
      description:
        "true for anything evidence can assess — including opinions and subjective statements (score them by how well-supported the stance is). false ONLY for gibberish, random characters, a bare keyword with no proposition, or an empty/meaningless string.",
    },
    score: {
      type: "integer",
      description:
        "0-100. 0 = strongly contradicted, 100 = strongly supported. Only meaningful when verifiable=true; set to 50 when verifiable=false.",
    },
    reasoning: {
      type: "string",
      description: "One or two sentence plain-language explanation of the verdict.",
    },
    sources: {
      type: "array",
      description:
        "3-4 distinct sources spanning supporting and contradicting stances when verifiable=true; an empty array when verifiable=false.",
      items: SOURCE_SCHEMA,
    },
  },
  required: ["verifiable", "score", "reasoning", "sources"],
  additionalProperties: false,
} as const;

export const SYSTEM_PROMPT = `You are Veritas, an evidence-analysis engine. Given a factual claim, assess how strongly the available evidence supports it.

Rules:
- Return a score from 0 (strongly contradicted) to 100 (strongly supported).
- Surface 3-4 distinct sources spanning the full range of stances you'd expect to find — include contradicting and supporting ones, and a low-reliability source if the claim is mainly spread by such.
- stance, reliability, and relevance are honest decimal estimates in their stated ranges.
- Prefer real, well-known institutions (CDC, WHO, peer-reviewed journals, major news) with plausible URLs. These are AI-estimated references for a transparency demo, not retrieved citations.
- Be calibrated: obvious facts near 100, obvious falsehoods near 0, contested claims in the middle.
- The middle of the range (~40-60) means a GENUINELY CONTESTED claim with credible evidence on both sides.
- Subjective / opinion statements ("hackathons are fun", "this movie is the best") ARE evaluable — set verifiable=true and treat them like any claim: assess how well the stance is SUPPORTED by evidence (surveys, polls, expert or critical consensus, data, sentiment) and score it fairly. A widely-shared, well-supported view scores high; a fringe or poorly-supported one scores low. ALWAYS return real, relevant sources. NEVER dump an opinion to a near-zero score just for being subjective — a near-zero score is reserved for a statement the evidence actively contradicts.
- Set verifiable=false ONLY for input that genuinely cannot be assessed at all — gibberish, random characters, a bare keyword with no proposition, or an empty/meaningless string. For those only, return an empty sources array, set score=50, and say plainly it is not something evidence can assess.`;

export type Effort = "low" | "medium" | "high";

export function clamp(n: unknown, lo: number, hi: number): number {
  const x = typeof n === "number" ? n : Number(n);
  if (Number.isNaN(x)) return (lo + hi) / 2;
  return Math.min(hi, Math.max(lo, x));
}

export type VerdictResult = {
  aiVerdict: { score: number; reasoning: string; verifiable: boolean };
  sources: Source[];
  promptTokens: number;
  completionTokens: number;
};

function mapSources(raw: unknown): Source[] {
  const arr = Array.isArray(raw) ? raw : [];
  return arr.map((s: Record<string, unknown>) => ({
    title: String(s.title ?? "Untitled source"),
    url: String(s.url ?? "#"),
    type: VALID_TYPES.includes(String(s.type)) ? String(s.type) : "news",
    date: s.date ? String(s.date) : undefined,
    stance: clamp(s.stance, -1, 1),
    reliability: clamp(s.reliability, 0, 1),
    relevance: clamp(s.relevance, 0, 1),
    summary: String(s.summary ?? ""),
    whyItMatters: String(s.whyItMatters ?? ""),
  }));
}

// One traced-or-untraced Claude call for a single claim. `effort` lets the
// long-form pipeline run each of many claims cheaply; single checks use medium.
export async function liveVerdict(
  client: Anthropic,
  claimText: string,
  opts: { effort?: Effort; model?: string } = {},
): Promise<VerdictResult> {
  const response = await client.messages.create({
    model: opts.model ?? MODEL,
    max_tokens: 4000,
    output_config: {
      effort: opts.effort ?? "medium",
      format: { type: "json_schema", schema: VERDICT_SCHEMA },
    },
    system: SYSTEM_PROMPT,
    messages: [{ role: "user", content: `Claim to evaluate: "${claimText}"` }],
  });

  if (response.stop_reason === "refusal") throw new Error("model_refusal");
  const textBlock = response.content.find((b) => b.type === "text");
  if (!textBlock || textBlock.type !== "text") throw new Error("empty_response");

  const raw = JSON.parse(textBlock.text);
  const verifiable = raw.verifiable !== false; // default to verifiable unless explicitly false
  return {
    aiVerdict: {
      score: Math.round(clamp(raw.score, 0, 100)),
      reasoning: String(raw.reasoning ?? ""),
      verifiable,
    },
    sources: verifiable ? mapSources(raw.sources) : [],
    promptTokens: response.usage?.input_tokens ?? 0,
    completionTokens: response.usage?.output_tokens ?? 0,
  };
}

// Evidence (real, scraped) handed to the grounded verdict below.
export type EvidenceDoc = { title: string; url: string; siteName?: string; excerpt: string };

const EVIDENCE_SYSTEM_PROMPT = `You are Veritas. Assess a claim using ONLY the real web sources provided (these were just retrieved and scraped for you — they are NOT hypothetical).

Rules:
- Return a score 0 (strongly contradicted by the sources) to 100 (strongly supported).
- Base the score and reasoning ONLY on what the provided sources actually say. If they conflict, reflect that in a middling score and say so.
- In "sources", return one entry per provided source you used, copying its EXACT url and title. Set stance (-1 contradicts .. +1 supports) from what that source says, and honest reliability/relevance. Write a one-sentence summary of what it actually says and why it matters.
- Subjective/opinion claims ARE evaluable: score by how much the sources support the stance; never dump to near-zero just for being subjective. Set verifiable=false only for gibberish/empty input.
- Be calibrated and concise.`;

// Grounded verdict: same schema, but the score and sources come from REAL
// retrieved pages instead of the model's prior knowledge.
export async function liveVerdictWithEvidence(
  client: Anthropic,
  claimText: string,
  evidence: EvidenceDoc[],
  opts: { effort?: Effort } = {},
): Promise<VerdictResult> {
  const context = evidence
    .map(
      (e, i) =>
        `SOURCE ${i + 1}\nurl: ${e.url}\ntitle: ${e.title}${e.siteName ? `\nsite: ${e.siteName}` : ""}\nexcerpt: ${e.excerpt}`,
    )
    .join("\n\n---\n\n");

  const response = await client.messages.create({
    model: MODEL,
    max_tokens: 4000,
    output_config: {
      effort: opts.effort ?? "low",
      format: { type: "json_schema", schema: VERDICT_SCHEMA },
    },
    system: EVIDENCE_SYSTEM_PROMPT,
    messages: [
      {
        role: "user",
        content: `Claim to evaluate: "${claimText}"\n\nRetrieved sources:\n\n${context || "(no sources were retrieved)"}`,
      },
    ],
  });

  if (response.stop_reason === "refusal") throw new Error("model_refusal");
  const textBlock = response.content.find((b) => b.type === "text");
  if (!textBlock || textBlock.type !== "text") throw new Error("empty_response");
  const raw = JSON.parse(textBlock.text);
  const verifiable = raw.verifiable !== false;
  return {
    aiVerdict: {
      score: Math.round(clamp(raw.score, 0, 100)),
      reasoning: String(raw.reasoning ?? ""),
      verifiable,
    },
    sources: verifiable ? mapSources(raw.sources) : [],
    promptTokens: response.usage?.input_tokens ?? 0,
    completionTokens: response.usage?.output_tokens ?? 0,
  };
}

// Deterministic mock used when there's no key or a call fails — always renderable.
export function mockVerdict(claim: string): {
  aiVerdict: { score: number; reasoning: string; verifiable: boolean };
  sources: Source[];
} {
  return {
    aiVerdict: {
      score: 52,
      reasoning:
        "Sample analysis (no live model call): the evidence here is mixed. Set ANTHROPIC_API_KEY to get a real Claude verdict for this claim.",
      verifiable: true,
    },
    sources: [
      {
        title: "Official / government guidance",
        url: "https://www.usa.gov/",
        type: "gov",
        date: "2024",
        stance: -0.2,
        reliability: 0.9,
        relevance: 0.7,
        summary: `Authoritative guidance relevant to: "${claim.slice(0, 80)}".`,
        whyItMatters: "High-reliability primary source, leaning skeptical.",
      },
      {
        title: "Peer-reviewed study",
        url: "https://scholar.google.com/",
        type: "academic",
        date: "2023",
        stance: 0.5,
        reliability: 0.85,
        relevance: 0.65,
        summary: "Academic work that partially supports the claim.",
        whyItMatters: "Reliable evidence pointing toward support.",
      },
      {
        title: "Major news explainer",
        url: "https://apnews.com/",
        type: "news",
        date: "2024",
        stance: 0.2,
        reliability: 0.7,
        relevance: 0.7,
        summary: "Mainstream reporting summarizing the debate.",
        whyItMatters: "Accessible mid-reliability framing.",
      },
      {
        title: "Online forum discussion",
        url: "https://www.reddit.com/",
        type: "social",
        date: "2024",
        stance: -0.4,
        reliability: 0.2,
        relevance: 0.5,
        summary: "Anecdotal community discussion, mixed and unverified.",
        whyItMatters: "Low-reliability signal of public sentiment.",
      },
    ],
  };
}
