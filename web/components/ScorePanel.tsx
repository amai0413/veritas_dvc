import type { Claim } from "@/lib/types";
import { STATUS_BLURB, analyzeVotes } from "@/lib/scoring";
import { scoreColor, CROWD_COLOR } from "@/lib/display";
import VerdictBadge from "./VerdictBadge";

function Meter({
  label,
  value,
  color,
  caption,
}: {
  label: string;
  value: number; // 0-100
  color: string;
  caption?: string;
}) {
  return (
    <div>
      <div className="flex items-baseline justify-between text-sm">
        <span className="font-medium text-slate-700">{label}</span>
        <span className="font-mono font-semibold" style={{ color }}>
          {Math.round(value)}
        </span>
      </div>
      <div className="mt-1 h-2 w-full overflow-hidden rounded-full bg-slate-200/70">
        <div
          className="h-full rounded-full transition-all"
          style={{ width: `${Math.max(2, value)}%`, backgroundColor: color }}
        />
      </div>
      {caption ? <p className="mt-1 text-xs text-slate-500">{caption}</p> : null}
    </div>
  );
}

// "Bottom line first" — the verdict badge, the blended score, and one sentence.
export default function ScorePanel({ claim }: { claim: Claim }) {
  const final = claim.finalScore;
  const isOpinion = claim.status === "opinion";
  const color = isOpinion ? "#7c3aed" : scoreColor(final);
  const { weights, voteCount } = analyzeVotes(claim.votes);
  const hasVotes = voteCount > 0;

  return (
    <section className="relative overflow-hidden rounded-3xl border border-white/60 bg-white/85 p-6 backdrop-blur-2xl shadow-[0_24px_70px_-28px_rgba(55,75,135,0.5),0_2px_6px_-3px_rgba(55,75,135,0.12)] before:pointer-events-none before:absolute before:inset-x-6 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-white before:to-transparent">
      <div className="flex flex-col gap-5 sm:flex-row sm:items-center sm:justify-between">
        <div className="min-w-0">
          <p className="text-xs font-semibold uppercase tracking-wider text-slate-400">Claim</p>
          <p className="mt-1 text-lg font-semibold leading-snug text-slate-900">
            &ldquo;{claim.text}&rdquo;
          </p>
          <div className="mt-3 flex items-center gap-3">
            <VerdictBadge status={claim.status} />
            <span className="text-sm text-slate-500">{STATUS_BLURB[claim.status]}</span>
          </div>
        </div>

        {/* Final blended credibility score */}
        <div className="flex shrink-0 items-center gap-4">
          <div
            className="flex h-24 w-24 flex-col items-center justify-center rounded-full border-4 bg-white/60"
            style={{ borderColor: color, boxShadow: `0 0 30px -12px ${color}` }}
          >
            <span className="text-3xl font-bold tabular-nums" style={{ color }}>
              {isOpinion ? "—" : Math.round(final)}
            </span>
            <span className="text-[10px] font-medium uppercase tracking-wide text-slate-400">
              {isOpinion ? "no score" : "/ 100"}
            </span>
          </div>
        </div>
      </div>

      <p className="mt-4 rounded-xl bg-white/60 p-3 text-sm leading-relaxed text-slate-700">
        {claim.aiVerdict.reasoning}
      </p>

      {isOpinion ? (
        <p className="mt-4 rounded-xl border border-violet-200 bg-violet-50/60 p-3 text-sm leading-relaxed text-violet-800">
          Veritas only scores verifiable factual claims. This input is a subjective opinion, so
          there&rsquo;s nothing to verify against evidence — no credibility score is shown.
        </p>
      ) : (
      <>
      <div className="mt-5 grid gap-4 sm:grid-cols-2">
        <Meter
          label="AI evidence score"
          value={claim.aiVerdict.score}
          color="#2563eb"
          caption={`Weight ${(weights.ai * 100).toFixed(0)}% · ${claim.sources.length} sources`}
        />
        <Meter
          label="Crowd consensus"
          value={claim.crowdScore}
          color={CROWD_COLOR}
          caption={
            hasVotes
              ? `Weight ${(weights.crowd * 100).toFixed(0)}% · ${voteCount} votes`
              : "No votes yet — score is AI-only until people weigh in"
          }
        />
      </div>

      <p className="mt-4 text-xs text-slate-400">
        Final = {(weights.ai * 100).toFixed(0)}% × AI ({claim.aiVerdict.score}) +{" "}
        {(weights.crowd * 100).toFixed(0)}% × Crowd ({Math.round(claim.crowdScore)}). Crowd weight
        rises with vote volume.
      </p>
      </>
      )}
    </section>
  );
}
