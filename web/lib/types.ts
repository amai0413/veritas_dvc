// Phase 1 data shapes — strictly matching the architecture document.

export type Source = {
  title: string;
  url: string;
  type: string;
  date?: string;
  stance: number; // -1 contradicts ... +1 supports
  reliability: number; // 0 low ... 1 high
  relevance: number; // 0 ... 1 (drives circle size)
  summary: string;
  whyItMatters: string;
};

export type VoteVerdict = "true" | "false";
export type Vote = { verdict: VoteVerdict };

export type ClaimStatus = "verified" | "false" | "disputed" | "uncertain" | "opinion";

export type Claim = {
  text: string;
  aiVerdict: { score: number; reasoning: string; verifiable: boolean };
  sources: Source[];
  // eslint-disable-next-line @typescript-eslint/no-explicit-any
  votes: { trusted: any[]; public: any[] };
  crowdScore: number;
  finalScore: number;
  status: ClaimStatus;
};

// The shape before scoring is applied (no crowdScore/finalScore/status yet).
export type ClaimInput = Pick<Claim, "text" | "aiVerdict" | "sources" | "votes">;

// Known source categories used for coloring the map (Source.type stays a string).
export type SourceType = "gov" | "academic" | "news" | "blog" | "social";

// ---- Long-form analysis (Phase A) ----
// A document is decomposed into individual checkable claims, each scored by the
// same verdict engine, then aggregated into a document-level credibility score.

// A claim pulled out of a longer document, before any verdict/scoring.
export type ExtractedClaim = {
  text: string; // self-contained, restated claim
  quote: string; // shortest verbatim excerpt it came from (for highlighting)
  importance: number; // 0..1 — how central to the document's thesis
};

// A fully-scored claim plus where it sits in the source document.
export type DocumentClaim = Claim & Pick<ExtractedClaim, "quote" | "importance">;

// Where an analyzed document came from, when fetched from a URL.
export type DocumentSource = {
  title: string;
  url: string;
  siteName?: string;
};

export type DocumentAnalysis = {
  documentScore: number; // 0-100, importance-weighted across claims
  summary: string; // one-line read on the document's overall factual thesis
  claims: DocumentClaim[];
  counts: Record<ClaimStatus, number>;
  note?: string; // caveat when the text is mostly opinion / has few checkable facts
  source?: DocumentSource; // present when analyzed from a fetched URL
};
