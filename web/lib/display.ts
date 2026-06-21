// Shared visual vocabulary so the map, legend, and cards stay in sync.

import type { ClaimStatus, SourceType } from "./types";

export const SOURCE_META: Record<SourceType, { label: string; color: string }> = {
  gov: { label: "Gov / Regulator", color: "#2563eb" }, // blue
  academic: { label: "Academic", color: "#0891b2" }, // cyan
  news: { label: "News", color: "#f59e0b" }, // amber
  blog: { label: "Blog", color: "#64748b" }, // slate
  social: { label: "Social", color: "#db2777" }, // pink
};

// The crowd marker is intentionally a distinct, bold color.
export const CROWD_COLOR = "#7c3aed"; // violet

// Source.type is a free-form string in the spec; look up its visual meta with a
// safe fallback for any unrecognized category.
export function sourceMeta(type: string): { label: string; color: string } {
  return SOURCE_META[type as SourceType] ?? { label: type || "Other", color: "#94a3b8" };
}

export const STATUS_META: Record<
  ClaimStatus,
  { label: string; chip: string; text: string; accent: string }
> = {
  verified: {
    label: "Verified",
    chip: "bg-emerald-50 border-emerald-200",
    text: "text-emerald-700",
    accent: "#059669",
  },
  false: {
    label: "False",
    chip: "bg-rose-50 border-rose-200",
    text: "text-rose-700",
    accent: "#e11d48",
  },
  disputed: {
    label: "Disputed",
    chip: "bg-amber-50 border-amber-200",
    text: "text-amber-800",
    accent: "#d97706",
  },
  uncertain: {
    label: "Uncertain",
    chip: "bg-slate-100 border-slate-200",
    text: "text-slate-600",
    accent: "#64748b",
  },
  opinion: {
    label: "Unverifiable",
    chip: "bg-violet-50 border-violet-200",
    text: "text-violet-700",
    accent: "#7c3aed",
  },
};

// Color a 0-100 credibility score along a red -> amber -> green ramp.
export function scoreColor(score: number): string {
  if (score >= 66) return "#059669"; // green
  if (score >= 40) return "#d97706"; // amber
  return "#e11d48"; // red
}
