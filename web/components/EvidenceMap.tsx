import type { Claim } from "@/lib/types";
import { sourceMeta, CROWD_COLOR, SOURCE_META } from "@/lib/display";
import { analyzeVotes } from "@/lib/scoring";

// The 2D evidence map. Each source is a circle:
//   X = stance   (left contradicts ... right supports)
//   Y = reliability (bottom low ... top high)
//   color = source type
//   size = relevance to the claim
// The crowd consensus is plotted as one large distinct marker on the SAME map,
// so a single picture shows both pillars: AI-found evidence + human consensus.

const VIEW_W = 640;
const VIEW_H = 470;
const X0 = 62;
const X1 = 616;
const Y0 = 22;
const Y1 = 404;
const PLOT_W = X1 - X0;
const PLOT_H = Y1 - Y0;

const xFor = (stance: number) => X0 + ((stance + 1) / 2) * PLOT_W;
const yFor = (reliability: number) => Y0 + (1 - reliability) * PLOT_H;
const rFor = (relevance: number) => 7 + relevance * 16;

export default function EvidenceMap({
  claim,
  selectedIndex,
  onSelect,
}: {
  claim: Claim;
  selectedIndex: number | null;
  onSelect: (index: number) => void;
}) {
  const sources = claim.sources;
  const { voteCount, confidence } = analyzeVotes(claim.votes);
  const showCrowd = voteCount > 0;
  const crowdStance = (claim.crowdScore / 100) * 2 - 1;
  const crowdX = xFor(crowdStance);
  const crowdY = yFor(confidence);

  return (
    <section className="relative overflow-hidden rounded-3xl border border-white/60 bg-white/85 p-5 backdrop-blur-2xl shadow-[0_24px_70px_-28px_rgba(55,75,135,0.5),0_2px_6px_-3px_rgba(55,75,135,0.12)] before:pointer-events-none before:absolute before:inset-x-6 before:top-0 before:h-px before:bg-gradient-to-r before:from-transparent before:via-white before:to-transparent">
      <div className="mb-3 flex items-center justify-between">
        <h3 className="text-sm font-semibold text-slate-900">Evidence map</h3>
        <p className="text-xs text-slate-400">Click any point for details</p>
      </div>

      <svg
        viewBox={`0 0 ${VIEW_W} ${VIEW_H}`}
        className="h-auto w-full select-none"
        role="img"
        aria-label="2D map of evidence by stance and reliability"
      >
        {/* Quadrant background tint */}
        <rect x={X0} y={Y0} width={PLOT_W} height={PLOT_H} fill="rgba(255,255,255,0.55)" rx="8" />

        {/* Gridlines */}
        {[0.25, 0.5, 0.75].map((t) => (
          <line
            key={`h${t}`}
            x1={X0}
            x2={X1}
            y1={Y0 + t * PLOT_H}
            y2={Y0 + t * PLOT_H}
            stroke="#e2e8f0"
            strokeWidth="1"
          />
        ))}
        {[0.25, 0.5, 0.75].map((t) => (
          <line
            key={`v${t}`}
            x1={X0 + t * PLOT_W}
            x2={X0 + t * PLOT_W}
            y1={Y0}
            y2={Y1}
            stroke="#e2e8f0"
            strokeWidth="1"
          />
        ))}

        {/* Center stance axis (stance = 0) emphasized */}
        <line
          x1={xFor(0)}
          x2={xFor(0)}
          y1={Y0}
          y2={Y1}
          stroke="#cbd5e1"
          strokeWidth="1.5"
          strokeDasharray="4 4"
        />

        {/* Axis labels */}
        <text x={X0} y={Y1 + 22} fontSize="12" fill="#e11d48" fontWeight="600">
          ◄ Contradicts
        </text>
        <text x={X1} y={Y1 + 22} fontSize="12" fill="#059669" fontWeight="600" textAnchor="end">
          Supports ►
        </text>
        <text x={(X0 + X1) / 2} y={Y1 + 38} fontSize="11" fill="#94a3b8" textAnchor="middle">
          Stance
        </text>

        <text
          x={16}
          y={Y0 + 6}
          fontSize="11"
          fill="#94a3b8"
          transform={`rotate(-90 16 ${Y0 + 6})`}
        >
          High reliability
        </text>
        <text
          x={16}
          y={Y1}
          fontSize="11"
          fill="#94a3b8"
          transform={`rotate(-90 16 ${Y1})`}
          textAnchor="end"
        >
          Low
        </text>

        {/* Source circles */}
        {sources.map((s, i) => {
          const cx = xFor(s.stance);
          const cy = yFor(s.reliability);
          const r = rFor(s.relevance);
          const isSel = selectedIndex === i;
          return (
            <g
              key={i}
              className="cursor-pointer"
              onClick={() => onSelect(i)}
              role="button"
              aria-label={`${s.title} — ${sourceMeta(s.type).label}`}
            >
              <circle
                cx={cx}
                cy={cy}
                r={r}
                fill={sourceMeta(s.type).color}
                fillOpacity={isSel ? 0.95 : 0.7}
                stroke={isSel ? "#0f172a" : "#ffffff"}
                strokeWidth={isSel ? 3 : 1.5}
              />
            </g>
          );
        })}

        {/* Crowd consensus marker — the second pillar on the same map */}
        {showCrowd ? (
          <g aria-label="Crowd consensus">
            <circle cx={crowdX} cy={crowdY} r={20} fill={CROWD_COLOR} fillOpacity={0.18} />
            <circle
              cx={crowdX}
              cy={crowdY}
              r={11}
              fill={CROWD_COLOR}
              stroke="#ffffff"
              strokeWidth={3}
            />
            <text
              x={crowdX}
              y={crowdY - 26}
              fontSize="11"
              fontWeight="700"
              fill={CROWD_COLOR}
              textAnchor="middle"
            >
              CROWD
            </text>
          </g>
        ) : null}
      </svg>

      {/* Legend */}
      <div className="mt-3 flex flex-wrap items-center gap-x-4 gap-y-2 text-xs text-slate-600">
        {Object.entries(SOURCE_META).map(([key, m]) => (
          <span key={key} className="inline-flex items-center gap-1.5">
            <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: m.color }} aria-hidden />
            {m.label}
          </span>
        ))}
        <span className="inline-flex items-center gap-1.5">
          <span className="h-2.5 w-2.5 rounded-full" style={{ backgroundColor: CROWD_COLOR }} aria-hidden />
          Crowd consensus
        </span>
        <span className="text-slate-400">· circle size = relevance</span>
      </div>
    </section>
  );
}
