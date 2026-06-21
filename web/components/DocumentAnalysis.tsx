"use client";

import { useMemo, useState } from "react";
import type {
  DocumentAnalysis as DocumentAnalysisData,
  DocumentClaim,
  VoteVerdict,
} from "@/lib/types";
import { VERDICT_LABEL, analyzeVotes, scoreClaim, scoreDocument } from "@/lib/scoring";
import { STATUS_META, scoreColor, sourceMeta } from "@/lib/display";
import EvidenceMap from "./EvidenceMap";

// Order the status breakdown by how much it should worry the reader.
const STATUS_ORDER = ["false", "disputed", "uncertain", "verified"] as const;

type VoteSet = DocumentClaim["votes"];

function ClaimRow({
  claim,
  onVote,
}: {
  claim: DocumentClaim;
  onVote: (verdict: VoteVerdict) => void;
}) {
  const [open, setOpen] = useState(false);
  const [selected, setSelected] = useState<number | null>(null);
  const meta = STATUS_META[claim.status];
  const color = scoreColor(claim.finalScore);
  const { voteCount, weights } = analyzeVotes(claim.votes);

  return (
    <li className="rounded-2xl border border-white/60 bg-white/85 backdrop-blur-xl transition hover:border-[#d8a93f]/30 hover:shadow-[0_10px_30px_-16px_rgba(70,90,150,0.4)]">
      <button
        type="button"
        onClick={() => setOpen((v) => !v)}
        aria-expanded={open}
        className="flex w-full items-start gap-3 p-4 text-left"
      >
        {/* score chip */}
        <span
          className="mt-0.5 flex h-10 w-10 shrink-0 flex-col items-center justify-center rounded-lg border-2 bg-white/60 text-sm font-bold tabular-nums"
          style={{ borderColor: color, color }}
        >
          {Math.round(claim.finalScore)}
        </span>

        <span className="min-w-0 flex-1">
          <span className="flex flex-wrap items-center gap-2">
            <span
              className={`inline-flex items-center gap-1.5 rounded-full border px-2 py-0.5 text-[11px] font-semibold ${meta.chip} ${meta.text}`}
            >
              <span
                className="h-2 w-2 rounded-full"
                style={{ backgroundColor: meta.accent }}
                aria-hidden
              />
              {VERDICT_LABEL[claim.status]}
            </span>
            {claim.importance >= 0.66 ? (
              <span className="rounded-full bg-slate-100 px-2 py-0.5 text-[11px] font-medium text-slate-500">
                Key claim
              </span>
            ) : null}
            {voteCount > 0 ? (
              <span className="rounded-full bg-violet-100 px-2 py-0.5 text-[11px] font-medium text-violet-700">
                {voteCount} {voteCount === 1 ? "vote" : "votes"}
              </span>
            ) : null}
            <span className="ml-auto text-xs text-slate-400">
              {claim.sources.length} sources · {open ? "Hide" : "Details"}
            </span>
          </span>

          <span className="mt-1.5 block text-sm font-semibold leading-snug text-slate-900">
            {claim.text}
          </span>
          {claim.quote ? (
            <span className="mt-1 block border-l-2 border-slate-200 pl-2 text-xs italic text-slate-500">
              &ldquo;{claim.quote}&rdquo;
            </span>
          ) : null}
        </span>
      </button>

      {open ? (
        <div className="border-t border-slate-100 px-4 py-3">
          <p className="rounded-lg bg-white/60 p-3 text-sm leading-relaxed text-slate-700">
            {claim.aiVerdict.reasoning}
          </p>

          {/* Per-claim voting — re-blends this claim and the document score live */}
          <div className="mt-3 flex flex-wrap items-center justify-between gap-2 rounded-lg border border-slate-200 p-3">
            <p className="text-xs text-slate-500">
              {voteCount > 0
                ? `AI ${Math.round(weights.ai * 100)}% · Crowd ${Math.round(weights.crowd * 100)}% — vote to shift the blend`
                : "No votes yet — score is AI-only. Add yours:"}
            </p>
            <div className="flex gap-2">
              <button
                type="button"
                onClick={() => onVote("true")}
                className="rounded-lg border border-emerald-300 bg-emerald-50 px-2.5 py-1 text-xs font-medium text-emerald-700 transition hover:bg-emerald-100"
              >
                👍 True
              </button>
              <button
                type="button"
                onClick={() => onVote("false")}
                className="rounded-lg border border-rose-300 bg-rose-50 px-2.5 py-1 text-xs font-medium text-rose-700 transition hover:bg-rose-100"
              >
                👎 False
              </button>
            </div>
          </div>

          {/* This claim's own 2D evidence map */}
          <div className="mt-3">
            <EvidenceMap claim={claim} selectedIndex={selected} onSelect={setSelected} />
          </div>

          {/* Compact source list */}
          <ul className="mt-3 grid gap-2 sm:grid-cols-2">
            {claim.sources.map((s, i) => {
              const sm = sourceMeta(s.type);
              const isSel = selected === i;
              return (
                <li
                  key={i}
                  className={`rounded-lg border bg-white/85 p-2.5 ${isSel ? "border-[#d8a93f]/60 ring-2 ring-[#d8a93f]/20" : "border-white/60"}`}
                >
                  <div className="flex items-center justify-between gap-2">
                    <span className="inline-flex items-center gap-1.5 text-[11px] font-medium text-slate-600">
                      <span
                        className="h-2 w-2 rounded-full"
                        style={{ backgroundColor: sm.color }}
                        aria-hidden
                      />
                      {sm.label}
                    </span>
                    <span className="text-[11px] text-slate-400">
                      rel {Math.round(s.reliability * 100)}%
                    </span>
                  </div>
                  <a
                    href={s.url}
                    target="_blank"
                    rel="noopener noreferrer"
                    className="mt-1 block text-xs font-semibold text-[#d8a93f] hover:text-[#b8862f] hover:underline"
                  >
                    {s.title} →
                  </a>
                  <p className="mt-0.5 text-xs leading-relaxed text-slate-500">{s.summary}</p>
                </li>
              );
            })}
          </ul>
        </div>
      ) : null}
    </li>
  );
}

export default function DocumentAnalysis({
  analysis,
  isMock,
}: {
  analysis: DocumentAnalysisData;
  isMock: boolean;
}) {
  // Per-claim votes live in state so voting re-blends each claim AND the document
  // score, exactly like single-claim mode. Initialized from the API's (empty) votes;
  // a new analysis remounts this component (keyed in the parent), resetting votes.
  const [votes, setVotes] = useState<VoteSet[]>(() => analysis.claims.map((c) => c.votes));
  const [showNote, setShowNote] = useState(false);

  const live = useMemo(() => {
    const claims: DocumentClaim[] = analysis.claims.map((c, i) => {
      const scored = scoreClaim({
        text: c.text,
        aiVerdict: c.aiVerdict,
        sources: c.sources,
        votes: votes[i] ?? { trusted: [], public: [] },
      });
      return { ...scored, quote: c.quote, importance: c.importance };
    });
    return scoreDocument(claims, analysis.summary);
  }, [analysis, votes]);

  function vote(index: number, verdict: VoteVerdict) {
    setVotes((prev) =>
      prev.map((v, i) =>
        i === index ? { trusted: v.trusted, public: [...v.public, { verdict }] } : v,
      ),
    );
  }

  const color = scoreColor(live.documentScore);
  const hasClaims = live.claims.length > 0;

  return (
    <div className="mt-8 flex flex-col gap-6">
      {isMock ? (
        <p className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
          Showing sample data — set <code>ANTHROPIC_API_KEY</code> for live claim extraction and
          verdicts.
        </p>
      ) : null}

      {/* Document-level bottom line */}
      <section className="relative overflow-hidden rounded-3xl border border-white/60 bg-white/85 p-6 backdrop-blur-2xl shadow-[0_24px_70px_-28px_rgba(55,75,135,0.5),0_2px_6px_-3px_rgba(55,75,135,0.12)] before:pointer-events-none before:absolute before:inset-x-6 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-white before:to-transparent">
        <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
          <div className="min-w-0">
            <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">
              Document credibility
            </p>
            {analysis.source ? (
              <a
                href={analysis.source.url}
                target="_blank"
                rel="noopener noreferrer"
                className="mt-1 block truncate text-sm font-semibold text-[#d8a93f] hover:text-[#b8862f] hover:underline"
                title={analysis.source.title}
              >
                {analysis.source.siteName ? `${analysis.source.siteName} · ` : ""}
                {analysis.source.title} →
              </a>
            ) : null}
            <p className="mt-1 text-base font-medium leading-snug text-slate-800">
              {analysis.summary}
            </p>
            <p className="mt-2 text-xs text-slate-500">
              {hasClaims
                ? `${live.claims.length} verifiable ${live.claims.length === 1 ? "claim" : "claims"} checked · importance-weighted, re-blended live as you vote`
                : "No verifiable factual claims to score"}
            </p>
          </div>
          {hasClaims ? (
            <div
              className="flex h-24 w-24 shrink-0 flex-col items-center justify-center rounded-full border-4 bg-white/60"
              style={{ borderColor: color, boxShadow: `0 0 30px -12px ${color}` }}
            >
              <span className="text-3xl font-bold tabular-nums" style={{ color }}>
                {Math.round(live.documentScore)}
              </span>
              <span className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
                / 100
              </span>
            </div>
          ) : (
            <div className="flex h-24 w-24 shrink-0 flex-col items-center justify-center rounded-full border-4 border-slate-200 bg-white/60">
              <span className="text-2xl font-bold text-slate-300">—</span>
            </div>
          )}
        </div>

        {/* Caveat when the text is mostly opinion — collapsed by default, one click to reveal */}
        {analysis.note ? (
          <div className="mt-4">
            <button
              type="button"
              onClick={() => setShowNote((v) => !v)}
              aria-expanded={showNote}
              className="inline-flex items-center gap-1 text-xs font-medium text-amber-700 hover:underline"
            >
              <span aria-hidden>ⓘ</span>
              Note on this document
              <span aria-hidden>{showNote ? "▲" : "▾"}</span>
            </button>
            {showNote ? (
              <p className="mt-2 rounded-lg border border-amber-200 bg-amber-50 p-3 text-sm text-amber-800">
                {analysis.note}
              </p>
            ) : null}
          </div>
        ) : null}

        {/* Status breakdown */}
        {hasClaims ? (
          <div className="mt-5 flex flex-wrap gap-2">
            {STATUS_ORDER.map((status) => {
              const n = live.counts[status];
              if (!n) return null;
              const m = STATUS_META[status];
              return (
                <span
                  key={status}
                  className={`inline-flex items-center gap-1.5 rounded-full border px-3 py-1 text-xs font-semibold ${m.chip} ${m.text}`}
                >
                  <span
                    className="h-2 w-2 rounded-full"
                    style={{ backgroundColor: m.accent }}
                    aria-hidden
                  />
                  {n} {VERDICT_LABEL[status]}
                </span>
              );
            })}
          </div>
        ) : null}
      </section>

      {/* Per-claim breakdown */}
      {hasClaims ? (
        <section>
          <h3 className="mb-3 text-sm font-semibold text-white/90">Claim-by-claim breakdown</h3>
          <ul className="flex flex-col gap-2.5">
            {live.claims.map((c, i) => (
              <ClaimRow key={i} claim={c} onVote={(verdict) => vote(i, verdict)} />
            ))}
          </ul>
        </section>
      ) : (
        <p className="rounded-2xl border border-white/60 bg-white/85 p-5 text-center text-sm text-slate-500 backdrop-blur-xl">
          This text is mostly opinion or commentary — there aren&rsquo;t clear factual claims to
          verify. Try an article or post that makes checkable statements.
        </p>
      )}

      {hasClaims ? (
        <p className="text-center text-xs text-white/40">
          Each claim is checked by the same evidence engine as single-claim mode; sources are
          AI-estimated references for this transparency demo.
        </p>
      ) : null}
    </div>
  );
}
