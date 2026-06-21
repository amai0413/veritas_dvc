// The scoring model — the heart of Veritas.
//
//   finalScore = w_ai * aiScore + w_crowd * crowdScore
//
// The crowd weight is NOT fixed: it scales with vote volume. With few votes the
// crowd weight drops toward 0 and the AI dominates; as votes accumulate, the
// crowd weight rises toward its cap (more votes = higher crowd weight). Trusted
// votes are weighted more heavily than public ones.
//
// Pure functions only — safe to import on both server and client.

import type {
  Claim,
  ClaimInput,
  ClaimStatus,
  DocumentAnalysis,
  DocumentClaim,
  Vote,
} from "./types";

export const TRUSTED_WEIGHT = 3; // a friend's vote counts more
export const PUBLIC_WEIGHT = 1;
export const MAX_CROWD_WEIGHT = 0.4; // -> AI 60% / crowd 40% at full confidence
export const VOTE_CONFIDENCE_K = 8; // half-saturation constant for vote volume

const HIGH = 60;
const LOW = 40;

// eslint-disable-next-line @typescript-eslint/no-explicit-any
type VoteSet = { trusted: any[]; public: any[] };

export interface VoteAnalysis {
  crowdScore: number; // 0-100, weighted by tier
  voteCount: number;
  weightedVotes: number;
  confidence: number; // 0-1, grows with weighted vote volume
  weights: { ai: number; crowd: number }; // effective blend weights
}

export function analyzeVotes(votes: VoteSet): VoteAnalysis {
  const tiers = [
    { list: votes?.trusted ?? [], w: TRUSTED_WEIGHT },
    { list: votes?.public ?? [], w: PUBLIC_WEIGHT },
  ];

  let weightedTotal = 0;
  let weightedTrue = 0;
  let voteCount = 0;

  for (const { list, w } of tiers) {
    for (const v of list as Vote[]) {
      weightedTotal += w;
      voteCount += 1;
      if (v?.verdict === "true") weightedTrue += w;
    }
  }

  // Confidence: 0 votes -> 0, lots of votes -> ~1.
  const rawConfidence = weightedTotal / (weightedTotal + VOTE_CONFIDENCE_K);
  const crowdScore = weightedTotal === 0 ? 50 : (weightedTrue / weightedTotal) * 100;

  // Crowd earns weight only as volume (confidence) grows; AI takes the rest.
  const crowd = voteCount === 0 ? 0 : MAX_CROWD_WEIGHT * rawConfidence;

  return {
    crowdScore,
    voteCount,
    weightedVotes: weightedTotal,
    confidence: voteCount === 0 ? 0 : rawConfidence,
    weights: { ai: 1 - crowd, crowd },
  };
}

function determineStatus(ai: number, crowd: number, voteCount: number): ClaimStatus {
  const aiHigh = ai >= HIGH;
  const aiLow = ai <= LOW;
  const crowdHigh = crowd >= HIGH;
  const crowdLow = crowd <= LOW;

  // No human input yet — lean on the AI, but stay honest about the middle.
  if (voteCount === 0) {
    if (aiHigh) return "verified";
    if (aiLow) return "false";
    return "uncertain";
  }

  // AI and crowd point opposite ways — our most interesting moment.
  if ((aiHigh && crowdLow) || (aiLow && crowdHigh)) return "disputed";
  if (aiHigh && crowdHigh) return "verified";
  if (aiLow && crowdLow) return "false";
  return "uncertain";
}

// Takes an unscored claim and returns a fully-scored Claim (exact spec shape).
export function scoreClaim(input: ClaimInput): Claim {
  const ai = input.aiVerdict.score;
  const a = analyzeVotes(input.votes);
  const finalScore = a.weights.ai * ai + a.weights.crowd * a.crowdScore;
  // Opinions / non-verifiable input get their own neutral status — never "false".
  const status =
    input.aiVerdict.verifiable === false
      ? "opinion"
      : determineStatus(ai, a.crowdScore, a.voteCount);

  return {
    ...input,
    crowdScore: a.crowdScore,
    finalScore,
    status,
  };
}

// Aggregate scored claims into a document-level credibility read. The document
// score is an importance-weighted mean of each claim's final score, so the
// claims central to the document's thesis move the needle most.
export function scoreDocument(claims: DocumentClaim[], summary: string): DocumentAnalysis {
  const counts: Record<ClaimStatus, number> = {
    verified: 0,
    false: 0,
    disputed: 0,
    uncertain: 0,
    opinion: 0,
  };

  let weightedScore = 0;
  let weightTotal = 0;
  for (const c of claims) {
    counts[c.status] += 1;
    const w = Math.max(0.01, c.importance);
    weightedScore += w * c.finalScore;
    weightTotal += w;
  }

  return {
    documentScore: weightTotal === 0 ? 50 : weightedScore / weightTotal,
    summary,
    claims,
    counts,
  };
}

// Bottom-line badge shown above the map.
export const VERDICT_LABEL: Record<ClaimStatus, string> = {
  verified: "Supported",
  false: "Contradicted",
  disputed: "Misleading",
  uncertain: "Uncertain",
  opinion: "Unverifiable",
};

export const STATUS_BLURB: Record<ClaimStatus, string> = {
  verified: "AI evidence and human consensus agree this is true.",
  false: "AI evidence and human consensus agree this is false.",
  disputed: "The AI and the crowd disagree — judge the evidence yourself.",
  uncertain: "The evidence is contested; the system is flagging this honestly.",
  opinion: "This input can't be assessed against evidence (e.g. gibberish or a bare keyword).",
};
