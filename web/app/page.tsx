"use client";

import { useMemo, useState } from "react";
import type { ClaimInput, DocumentAnalysis as DocumentAnalysisData, VoteVerdict } from "@/lib/types";
import { scoreClaim } from "@/lib/scoring";
import { DEMO_SCENARIOS } from "@/lib/demoData";
import ScorePanel from "@/components/ScorePanel";
import EvidenceMap from "@/components/EvidenceMap";
import SourceCard from "@/components/SourceCard";
import DocumentAnalysis from "@/components/DocumentAnalysis";
import VeritasMark from "@/components/VeritasMark";

type Mode = "claim" | "article";

const SAMPLE_ARTICLE = `The Great Wall of China is the only man-made structure visible from space with the naked eye. It stretches for over 13,000 miles and was built in a single dynasty to keep out invaders. Construction first began more than 2,000 years ago. Today the wall draws tens of millions of tourists every year, making it one of the most visited landmarks on Earth.`;

// shared liquid-glass surfaces — translucent, strong blur, bright specular top edge
const CARD =
  "relative overflow-hidden rounded-3xl border border-white/60 bg-white/85 backdrop-blur-2xl shadow-[0_24px_70px_-28px_rgba(55,75,135,0.5),0_2px_6px_-3px_rgba(55,75,135,0.12)] before:pointer-events-none before:absolute before:inset-x-6 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-white before:to-transparent";
const INPUT =
  "w-full rounded-xl border border-slate-300/70 bg-white/70 p-3 text-sm text-slate-900 outline-none transition placeholder:text-slate-400 focus:border-[#d8a93f]/60 focus:ring-2 focus:ring-[#d8a93f]/20";
const CTA =
  "inline-flex items-center justify-center gap-2 rounded-xl bg-gradient-to-br from-[#f0cf7a] to-[#d8a93f] px-4 py-2.5 text-sm font-semibold text-[#2a1e05] shadow-[0_8px_24px_-8px_rgba(216,169,63,0.65)] transition hover:brightness-[1.06] hover:shadow-[0_12px_32px_-8px_rgba(216,169,63,0.75)] disabled:cursor-not-allowed disabled:from-slate-300 disabled:to-slate-300 disabled:text-white/80 disabled:shadow-none";

export default function Home() {
  const [mode, setMode] = useState<Mode>("claim");

  // ---- single-claim mode ----
  const [input, setInput] = useState("");
  const [claim, setClaim] = useState<ClaimInput | null>(null);
  const [loading, setLoading] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [isMock, setIsMock] = useState(false);
  const [selectedIndex, setSelectedIndex] = useState<number | null>(null);

  // ---- article mode ----
  const [docInput, setDocInput] = useState("");
  const [urlInput, setUrlInput] = useState("");
  const [analysis, setAnalysis] = useState<DocumentAnalysisData | null>(null);
  const [docLoading, setDocLoading] = useState(false);
  const [docError, setDocError] = useState<string | null>(null);
  const [docMock, setDocMock] = useState(false);
  const [analyzeNonce, setAnalyzeNonce] = useState(0); // remounts results to reset per-claim votes

  const scored = useMemo(() => (claim ? scoreClaim(claim) : null), [claim]);

  async function checkClaim() {
    const text = input.trim();
    if (!text || loading) return;
    setLoading(true);
    setError(null);
    try {
      const res = await fetch("/api/check", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ claim: text }),
      });
      const data = await res.json();
      if (!res.ok) {
        setError(data.error ?? "Something went wrong.");
        return;
      }
      setClaim({
        text,
        aiVerdict: data.aiVerdict,
        sources: data.sources ?? [],
        votes: { trusted: [], public: [] },
      });
      setIsMock(Boolean(data.mock));
      setSelectedIndex(null);
    } catch {
      setError("Network error — could not reach the analysis endpoint.");
    } finally {
      setLoading(false);
    }
  }

  async function runAnalyze(body: { document?: string; url?: string }) {
    setDocLoading(true);
    setDocError(null);
    try {
      const res = await fetch("/api/analyze", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(body),
      });
      const data = await res.json();
      if (!res.ok) {
        setDocError(data.error ?? "Something went wrong.");
        return;
      }
      setAnalysis(data);
      setDocMock(Boolean(data.mock));
      setAnalyzeNonce((n) => n + 1);
    } catch {
      setDocError("Network error — could not reach the analysis endpoint.");
    } finally {
      setDocLoading(false);
    }
  }

  function analyzeDocument() {
    const text = docInput.trim();
    if (text.length < 40 || docLoading) return;
    runAnalyze({ document: text });
  }

  function analyzeLink() {
    const u = urlInput.trim();
    if (!u || docLoading) return;
    runAnalyze({ url: u });
  }

  function loadDemo(scenarioClaim: ClaimInput, text: string) {
    setClaim(structuredClone(scenarioClaim));
    setInput(text);
    setError(null);
    setIsMock(false);
    setSelectedIndex(null);
  }

  function addVote(verdict: VoteVerdict) {
    if (!claim) return;
    setClaim({
      ...claim,
      votes: {
        trusted: claim.votes.trusted,
        public: [...claim.votes.public, { verdict }],
      },
    });
  }

  return (
    <div className="relative min-h-screen w-full overflow-hidden bg-[#060607]">
      {/* Fixed brand backdrop — the page scrolls over the logo */}
      <div aria-hidden className="pointer-events-none fixed inset-0 flex items-center justify-center">
        <div
          className="absolute left-1/2 top-1/2 h-[820px] w-[820px] -translate-x-1/2 -translate-y-1/2 rounded-full blur-[140px]"
          style={{ background: "radial-gradient(circle, rgba(216,169,63,0.16), transparent 62%)" }}
        />
        <VeritasMark className="w-[min(78vw,560px)] opacity-[0.18]" />
      </div>

      {/* legibility vignette */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0"
        style={{ background: "radial-gradient(ellipse at 50% 42%, transparent 34%, rgba(0,0,0,0.6))" }}
      />

      {/* fine grain */}
      <div
        aria-hidden
        className="pointer-events-none fixed inset-0 opacity-[0.06] mix-blend-overlay"
        style={{
          backgroundImage:
            "url(\"data:image/svg+xml,%3Csvg xmlns='http://www.w3.org/2000/svg' width='140' height='140'%3E%3Cfilter id='n'%3E%3CfeTurbulence type='fractalNoise' baseFrequency='0.9' numOctaves='2' stitchTiles='stitch'/%3E%3C/filter%3E%3Crect width='100%25' height='100%25' filter='url(%23n)'/%3E%3C/svg%3E\")",
        }}
      />

      <main className="relative z-10 mx-auto w-full max-w-5xl px-4 py-10 sm:py-16">
        {/* Header — the brand mark lives in the backdrop; keep this lightweight */}
        <header className="mb-10 text-center">
          <VeritasMark className="mx-auto h-28 w-auto" />
          <p className="mt-3 text-[11px] font-semibold uppercase tracking-[0.5em] text-[#e0bd63]">
            The Human + AI Truth Engine
          </p>
          <p className="mx-auto mt-4 max-w-xl text-[15px] leading-relaxed text-white/60">
            Veritas fights misinformation by combining{" "}
            <span className="font-semibold text-white">AI speed</span> with{" "}
            <span className="font-semibold text-white">human judgment</span> — searching
            trustworthy sources, analyzing evidence with agents, and returning a transparent
            credibility score.
          </p>
        </header>

        {/* Mode toggle */}
        <div className="mb-6 flex justify-center">
          <div className="inline-flex rounded-full border border-white/70 bg-white/85 p-1 text-sm font-medium backdrop-blur-xl shadow-[0_6px_24px_-12px_rgba(70,90,150,0.4)]">
            {(["claim", "article"] as const).map((m) => (
              <button
                key={m}
                type="button"
                onClick={() => setMode(m)}
                className={`rounded-full px-5 py-1.5 transition-all ${
                  mode === m
                    ? "bg-white text-slate-900 shadow-[0_2px_10px_rgba(70,90,150,0.18)]"
                    : "text-slate-500 hover:text-slate-700"
                }`}
              >
                {m === "claim" ? "Single claim" : "Analyze article"}
              </button>
            ))}
          </div>
        </div>

        {/* ---- Single-claim mode ---- */}
        {mode === "claim" ? (
          <>
            <section className={`${CARD} p-5`}>
              <label htmlFor="claim" className="text-sm font-medium text-slate-700">
                Enter a claim to fact-check
              </label>
              <textarea
                id="claim"
                value={input}
                onChange={(e) => setInput(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === "Enter" && (e.metaKey || e.ctrlKey)) checkClaim();
                }}
                rows={2}
                placeholder="e.g. The Great Wall of China is visible from space with the naked eye."
                className={`mt-2 resize-none ${INPUT}`}
              />
              <div className="mt-3 flex flex-wrap items-center gap-3">
                <button type="button" onClick={checkClaim} disabled={loading || !input.trim()} className={CTA}>
                  {loading ? "Analyzing…" : "Check claim"}
                </button>
                <span className="text-xs text-slate-400">⌘/Ctrl + Enter</span>
              </div>

              {/* Demo scenarios */}
              <div className="mt-5 border-t border-white/60 pt-4">
                <p className="mb-2 text-xs font-semibold uppercase tracking-wider text-slate-400">
                  Or load a demo scenario
                </p>
                <div className="grid gap-2 sm:grid-cols-3">
                  {DEMO_SCENARIOS.map((s) => (
                    <button
                      key={s.id}
                      type="button"
                      onClick={() => loadDemo(s.claim, s.claim.text)}
                      className="rounded-2xl border border-white/60 bg-white/80 p-3 text-left backdrop-blur-xl transition hover:border-[#d8a93f]/40 hover:bg-white/75"
                    >
                      <span className="block text-sm font-semibold text-slate-800">{s.label}</span>
                      <span className="mt-0.5 block text-xs text-slate-500">{s.blurb}</span>
                    </button>
                  ))}
                </div>
              </div>

              {error ? (
                <p className="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
                  {error}
                </p>
              ) : null}
            </section>

            {scored ? (
              <div className="mt-8 flex flex-col gap-6">
                {isMock ? (
                  <p className="rounded-xl border border-amber-200 bg-amber-50 p-3 text-xs text-amber-800">
                    Showing sample data — set <code>ANTHROPIC_API_KEY</code> for a live Claude
                    verdict on typed claims.
                  </p>
                ) : null}

                <ScorePanel claim={scored} />

                {scored.status !== "opinion" ? (
                <>
                {/* Voting */}
                <section className={`${CARD} flex flex-wrap items-center justify-between gap-3 p-4`}>
                  <div>
                    <p className="text-sm font-semibold text-slate-800">Add your vote to the crowd</p>
                    <p className="text-xs text-slate-500">Watch the crowd weight rise as votes come in.</p>
                  </div>
                  <div className="flex gap-2">
                    <button
                      type="button"
                      onClick={() => addVote("true")}
                      className="rounded-lg border border-emerald-300 bg-emerald-50 px-3 py-1.5 text-sm font-medium text-emerald-700 transition hover:bg-emerald-100"
                    >
                      👍 True
                    </button>
                    <button
                      type="button"
                      onClick={() => addVote("false")}
                      className="rounded-lg border border-rose-300 bg-rose-50 px-3 py-1.5 text-sm font-medium text-rose-700 transition hover:bg-rose-100"
                    >
                      👎 False
                    </button>
                  </div>
                </section>

                <EvidenceMap claim={scored} selectedIndex={selectedIndex} onSelect={setSelectedIndex} />

                {/* Source cards */}
                <section>
                  <h3 className="mb-3 text-sm font-semibold text-white/90">
                    Evidence sources ({scored.sources.length})
                  </h3>
                  <div className="grid gap-3 sm:grid-cols-2 lg:grid-cols-3">
                    {scored.sources.map((s, i) => (
                      <SourceCard
                        key={i}
                        source={s}
                        index={i}
                        selected={selectedIndex === i}
                        onSelect={setSelectedIndex}
                      />
                    ))}
                  </div>
                </section>

                <p className="text-center text-xs text-white/40">
                  Sources shown for live checks are AI-estimated references for this transparency
                  demo, not retrieved citations.
                </p>
                </>
                ) : null}
              </div>
            ) : null}
          </>
        ) : null}

        {/* ---- Article mode ---- */}
        {mode === "article" ? (
          <>
            <section className={`${CARD} p-5`}>
              <label htmlFor="url" className="text-sm font-medium text-slate-700">
                Analyze a link
              </label>
              <p className="mt-0.5 text-xs text-slate-500">
                Paste a URL — Veritas fetches the article, breaks it into its key factual claims,
                checks each one, then scores the whole document.
              </p>
              <div className="mt-2 flex flex-col gap-2 sm:flex-row">
                <input
                  id="url"
                  type="url"
                  value={urlInput}
                  onChange={(e) => setUrlInput(e.target.value)}
                  onKeyDown={(e) => {
                    if (e.key === "Enter") analyzeLink();
                  }}
                  placeholder="https://example.com/news-article"
                  className={`min-w-0 flex-1 ${INPUT}`}
                />
                <button
                  type="button"
                  onClick={analyzeLink}
                  disabled={docLoading || !urlInput.trim()}
                  className={`shrink-0 ${CTA}`}
                >
                  {docLoading ? "Analyzing…" : "Fetch & analyze"}
                </button>
              </div>

              <div className="my-4 flex items-center gap-3 text-xs text-slate-400">
                <span className="h-px flex-1 bg-slate-300/60" />
                or paste text directly
                <span className="h-px flex-1 bg-slate-300/60" />
              </div>

              <label htmlFor="document" className="text-sm font-medium text-slate-700">
                Paste an article, post, or thread
              </label>
              <textarea
                id="document"
                value={docInput}
                onChange={(e) => setDocInput(e.target.value)}
                rows={7}
                placeholder="Paste a few paragraphs of text to fact-check…"
                className={`mt-2 resize-y ${INPUT}`}
              />
              <div className="mt-3 flex flex-wrap items-center gap-4">
                <button
                  type="button"
                  onClick={analyzeDocument}
                  disabled={docLoading || docInput.trim().length < 40}
                  className={CTA}
                >
                  {docLoading ? "Analyzing…" : "Analyze article"}
                </button>
                <button
                  type="button"
                  onClick={() => setDocInput(SAMPLE_ARTICLE)}
                  className="text-xs font-medium text-[#d8a93f] hover:text-[#b8862f] hover:underline"
                >
                  Load sample article
                </button>
              </div>

              {docError ? (
                <p className="mt-4 rounded-xl border border-rose-200 bg-rose-50 p-3 text-sm text-rose-700">
                  {docError}
                </p>
              ) : null}
            </section>

            {analysis ? (
              <DocumentAnalysis key={analyzeNonce} analysis={analysis} isMock={docMock} />
            ) : null}
          </>
        ) : null}

        <footer className="mt-14 text-center text-xs text-white/40">
          Veritas · AI verdict + human consensus + evidence map · long-form analysis
        </footer>
      </main>
    </div>
  );
}
