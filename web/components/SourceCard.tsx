import type { Source } from "@/lib/types";
import { sourceMeta } from "@/lib/display";

function stanceLabel(stance: number): { text: string; color: string } {
  if (stance > 0.25) return { text: "Supports", color: "#059669" };
  if (stance < -0.25) return { text: "Contradicts", color: "#e11d48" };
  return { text: "Mixed", color: "#64748b" };
}

function Bar({ label, value }: { label: string; value: number }) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-16 text-[11px] text-slate-500">{label}</span>
      <div className="h-1.5 flex-1 overflow-hidden rounded-full bg-slate-200/70">
        <div
          className="h-full rounded-full bg-slate-400"
          style={{ width: `${Math.round(value * 100)}%` }}
        />
      </div>
    </div>
  );
}

// Clickable evidence card. Selection is two-way bound with the map.
export default function SourceCard({
  source,
  index,
  selected,
  onSelect,
}: {
  source: Source;
  index: number;
  selected: boolean;
  onSelect: (index: number) => void;
}) {
  const meta = sourceMeta(source.type);
  const stance = stanceLabel(source.stance);

  return (
    <button
      type="button"
      onClick={() => onSelect(index)}
      aria-pressed={selected}
      className={`flex w-full flex-col gap-2 rounded-2xl border bg-white/85 p-4 text-left backdrop-blur-xl transition hover:bg-white/95 hover:shadow-[0_10px_30px_-14px_rgba(70,90,150,0.4)] focus:outline-none focus-visible:ring-2 focus-visible:ring-[#d8a93f]/40 ${
        selected ? "border-[#d8a93f]/60 ring-2 ring-[#d8a93f]/20" : "border-white/60"
      }`}
    >
      <div className="flex items-center justify-between gap-2">
        <span className="inline-flex items-center gap-1.5 text-xs font-medium text-slate-600">
          <span
            className="h-2.5 w-2.5 rounded-full"
            style={{ backgroundColor: meta.color }}
            aria-hidden
          />
          {meta.label}
        </span>
        <span
          className="rounded-full px-2 py-0.5 text-[11px] font-semibold"
          style={{ color: stance.color, backgroundColor: `${stance.color}1a` }}
        >
          {stance.text}
        </span>
      </div>

      <div>
        <h4 className="text-sm font-semibold leading-snug text-slate-900">{source.title}</h4>
        {source.date ? <p className="text-[11px] text-slate-400">{source.date}</p> : null}
      </div>

      <p className="text-xs leading-relaxed text-slate-600">{source.summary}</p>
      <p className="text-xs leading-relaxed text-slate-500">
        <span className="font-medium text-slate-600">Why it matters: </span>
        {source.whyItMatters}
      </p>

      <div className="mt-1 flex flex-col gap-1">
        <Bar label="Reliability" value={source.reliability} />
        <Bar label="Relevance" value={source.relevance} />
      </div>

      <a
        href={source.url}
        target="_blank"
        rel="noopener noreferrer"
        onClick={(e) => e.stopPropagation()}
        className="mt-1 text-xs font-medium text-[#d8a93f] hover:text-[#b8862f] hover:underline"
      >
        Visit source →
      </a>
    </button>
  );
}
