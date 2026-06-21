import type { ClaimStatus } from "@/lib/types";
import { VERDICT_LABEL } from "@/lib/scoring";
import { STATUS_META } from "@/lib/display";

// The bottom-line badge: a single word answer (Supported / Contradicted /
// Misleading / Uncertain) the user sees before the graph.
export default function VerdictBadge({ status }: { status: ClaimStatus }) {
  const meta = STATUS_META[status];
  return (
    <span
      className={`inline-flex items-center gap-2 rounded-full border px-4 py-1.5 text-sm font-semibold ${meta.chip} ${meta.text}`}
    >
      <span
        className="h-2.5 w-2.5 rounded-full"
        style={{ backgroundColor: meta.accent }}
        aria-hidden
      />
      {VERDICT_LABEL[status]}
    </span>
  );
}
